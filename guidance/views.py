import json
from datetime import timedelta
from decimal import Decimal
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import F
from django.http import JsonResponse, HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from .models import StudentSubscription, MentorProfile, DoubtTicket, PayoutTransaction
from .services import sync_student_to_aisensy, send_solution_to_student_whatsapp
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
import uuid
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile

from django.core import signing
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

PLAN_MAP = {
    "JEE_NORM_3M":  {"tier": "NORMAL", "days": 90,  "quota": 25},
    "JEE_PLUS_3M":  {"tier": "PLUS",   "days": 90,  "quota": 45},
    "JEE_NORM_12M": {"tier": "NORMAL", "days": 365, "quota": 25},
    "JEE_PLUS_12M": {"tier": "PLUS",   "days": 365, "quota": 45},
}


# ---------------------------------------------------------
# 1. RAZORPAY WEBHOOK (Auto-Enrolls Students & Top-Ups)
# ---------------------------------------------------------
@csrf_exempt
def razorpay_webhook(request):
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    data = json.loads(request.body)
    if data.get("event") == "payment.captured":
        entity = data["payload"]["payment"]["entity"]
        notes = entity.get("notes", {})
        phone = notes.get("whatsapp_number", entity.get("contact", "")).replace("+", "").strip()
        name = notes.get("student_name", "JEE Aspirant")
        plan_code = notes.get("plan_code", "JEE_NORM_3M")

        now = timezone.now()

        # Case A: ₹299 20-Doubt Booster Pack
        if plan_code == "BOOSTER_20":
            sub = get_object_or_404(StudentSubscription, phone_number=phone)
            sub.monthly_doubts_remaining += 20
            sub.save()
            sync_student_to_aisensy(sub)
            return JsonResponse({"status": "booster_credited", "remaining": sub.monthly_doubts_remaining})

        # Case B: 3-Month or 12-Month Plan
        cfg = PLAN_MAP.get(plan_code, PLAN_MAP["JEE_NORM_3M"])
        sub, _ = StudentSubscription.objects.update_or_create(
            phone_number=phone,
            defaults={
                "student_name": name,
                "tier": cfg["tier"],
                "duration_days": cfg["days"],
                "active_doubts": 0,
                "monthly_doubts_remaining": cfg["quota"],
                "next_quota_reset_date": now + timedelta(days=30),
                "expiry_date": now + timedelta(days=cfg["days"]),
            },
        )
        sync_student_to_aisensy(sub)
        return JsonResponse({"status": "subscription_activated", "tier": sub.tier})

    return JsonResponse({"status": "ignored"})


# ---------------------------------------------------------
# 2. AISENSY DOUBT INTAKE WEBHOOK (Called by Flow Builder)
# ---------------------------------------------------------
@csrf_exempt
def aisensy_create_doubt(request):
    """
    Triggered by AiSensy's 'API Request' block right after subject selection.
    Expected JSON: {"phone": "919876543210", "subject": "Chemistry", "image_url": "https://..."}
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    print("AI-SENSY DATA RECEIVED:", request.body.decode('utf-8'))

    body = json.loads(request.body)
    phone = str(body.get("phone", "")).replace("+", "").strip()
    subject = body.get("subject", "Chemistry")
    image_url = body.get("image_url", "")
    

    with transaction.atomic():
        student = get_object_or_404(
            StudentSubscription.objects.select_for_update(), phone_number=phone
        )

        if student.monthly_doubts_remaining <= 0:
            return JsonResponse({"error": "Monthly quota exhausted"}, status=403)

        if student.active_doubts >= student.max_concurrency:
            return JsonResponse({"error": "Concurrency limit reached"}, status=429)

        # Increment ActiveDoubts & create ticket in pool
        student.active_doubts += 1
        student.save()

        ticket = DoubtTicket.objects.create(
            student=student,
            subject=subject,
            tier_at_creation=student.tier,
            question_image_url=image_url,
            status="OPEN",
        )

    sync_student_to_aisensy(student, current_subject=subject)
    return JsonResponse({"status": "queued", "ticket_id": ticket.id, "active_doubts": student.active_doubts})

# ---------------------------------------------------------
# 3. ATOMIC MENTOR CLAIM ENDPOINT (First-to-Click Wins)
# ---------------------------------------------------------
@csrf_exempt
# @login_required
@csrf_exempt
def claim_doubt_ticket(request, ticket_id):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    # 1. Get mentor via custom token (This replaces the request.user check)
    mentor = get_authenticated_mentor(request)
    if not mentor:
        return JsonResponse({"error": "Unauthorized"}, status=401)

    release_stale_claims()

    with transaction.atomic():
        ticket = get_object_or_404(DoubtTicket.objects.select_for_update(), id=ticket_id)

        if ticket.status != "OPEN":
            return JsonResponse(
                {"error": f"Too slow! Doubt #{ticket.id} was just claimed by another mentor."},
                status=409,
            )

        ticket.status = "CLAIMED"
        ticket.claimed_by = mentor
        ticket.claimed_at = timezone.now()
        ticket.save()

    return JsonResponse({
        "status": "claimed",
        "ticket_id": ticket.id,
        "deadline_minutes": 15,
    })

# ---------------------------------------------------------
# 4. MENTOR RESOLVE & INSTANT BOUNTY PAYOUT ENDPOINT
# ---------------------------------------------------------
@csrf_exempt
# @login_required
@csrf_exempt
def resolve_doubt_ticket(request, ticket_id):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    # 1. Get mentor via custom token (This replaces the request.user check)
    mentor = get_authenticated_mentor(request)
    if not mentor:
        return JsonResponse({"error": "Unauthorized"}, status=401)

    body = json.loads(request.body) if request.content_type == "application/json" else request.POST

    solution_url = body.get("solution_video_url", "").strip()
    notes = body.get("solution_notes", "Solved step-by-step via video.")

    with transaction.atomic():
        ticket = get_object_or_404(
            DoubtTicket.objects.select_for_update(),
            id=ticket_id,
            claimed_by=mentor,
            status="CLAIMED",
        )
        student = StudentSubscription.objects.select_for_update().get(id=ticket.student_id)

        ticket.resolved_at = timezone.now()
        ticket.solution_video_url = solution_url 
        ticket.solution_notes = notes
        ticket.status = "RESOLVED"

        # Safe payout calculation
        payout = Decimal(str(ticket.calculate_payout())) if hasattr(ticket, 'calculate_payout') else Decimal(str(mentor.bounty_per_doubt))
        ticket.bounty_earned = payout
        ticket.save()

        # Credit Mentor Wallet
        mentor.wallet_balance = F("wallet_balance") + payout
        mentor.total_solved = F("total_solved") + 1
        mentor.save()
        mentor.refresh_from_db()

        PayoutTransaction.objects.create(
            mentor=mentor,
            ticket=ticket,
            tx_type="BOUNTY",
            amount=payout,
            notes=f"Solved {ticket.subject} ({ticket.tier_at_creation}) Doubt #{ticket.id}",
        )

        # Unlock Student Queue Slot & Deduct 1 Monthly Quota
        student.active_doubts = max(0, student.active_doubts - 1)
        student.monthly_doubts_remaining = max(0, student.monthly_doubts_remaining - 1)
        student.save()

    # Push solution & updated attributes back to AiSensy (8893209106)
    send_solution_to_student_whatsapp(student, ticket)
    sync_student_to_aisensy(student, current_subject=ticket.subject)

    return JsonResponse({
        "status": "resolved",
        "bounty_credited": float(payout),
        "new_wallet_balance": float(mentor.wallet_balance),
    })

# ---------------------------------------------------------
# 5. ANTI-HOARDING & DAILY LIFECYCLE CRON
# ---------------------------------------------------------
def release_stale_claims():
    """Strips claims older than 15 minutes and applies a ₹10 penalty to prevent hoarding."""
    cutoff = timezone.now() - timedelta(minutes=15)
    stale_tickets = DoubtTicket.objects.select_for_update().filter(
        status="CLAIMED", claimed_at__lte=cutoff
    )
    with transaction.atomic():
        for ticket in stale_tickets:
            mentor = ticket.claimed_by
            if mentor:
                penalty = Decimal("15.00") if ticket.tier_at_creation == "PLUS" else Decimal("10.00")
                mentor.wallet_balance = F("wallet_balance") - penalty
                mentor.save()
                PayoutTransaction.objects.create(
                    mentor=mentor,
                    ticket=ticket,
                    tx_type="PENALTY",
                    amount=-penalty,
                    notes=f"15-min timeout exceeded on Doubt #{ticket.id}",
                )
            ticket.status = "OPEN"
            ticket.claimed_by = None
            ticket.claimed_at = None
            ticket.save()


@csrf_exempt
def daily_lifecycle_cron(request):
    """Call once daily via GitHub Actions or Render Cron to reset 30-day quotas & expire plans."""
    if request.headers.get("X-Cron-Token") != settings.CRON_SECRET_TOKEN:
        return JsonResponse({"error": "Unauthorized"}, status=401)

    now = timezone.now()
    release_stale_claims()

    # 1. Expire finished 90-day or 365-day subscriptions
    expired_subs = StudentSubscription.objects.filter(expiry_date__lte=now).exclude(tier="EXPIRED")
    exp_count = 0
    for sub in expired_subs:
        sub.tier = "EXPIRED"
        sub.monthly_doubts_remaining = 0
        sub.active_doubts = 0
        sub.save()
        sync_student_to_aisensy(sub)
        exp_count += 1

    # 2. Refresh 30-day monthly doubt quotas for active 3M and 12M students
    reset_subs = StudentSubscription.objects.filter(
        next_quota_reset_date__lte=now, expiry_date__gt=now
    ).exclude(tier="EXPIRED")
    reset_count = 0
    for sub in reset_subs:
        sub.monthly_doubts_remaining = sub.base_monthly_quota
        sub.next_quota_reset_date = now + timedelta(days=30)
        sub.save()
        sync_student_to_aisensy(sub)
        reset_count += 1

    return JsonResponse({"expired_count": exp_count, "quota_reset_count": reset_count})


# ---------------------------------------------------------
# 6. BUILT-IN MENTOR BOUNTY PORTAL (HTML View)
# ---------------------------------------------------------
@login_required
def mentor_bounty_board(request):
    """Lightweight Tailwind portal where mentors claim & resolve doubts without needing AiSensy seats."""
    release_stale_claims()
    mentor = get_object_or_404(MentorProfile, user=request.user)
    open_tickets = DoubtTicket.objects.filter(
        status="OPEN", subject=mentor.primary_subject
    ).order_by("-tier_at_creation", "created_at")
    my_claimed = DoubtTicket.objects.filter(status="CLAIMED", claimed_by=mentor)

    cards_html = ""
    for t in open_tickets:
        bounty_str = "₹20 (PLUS VIP)" if t.tier_at_creation == "PLUS" else "₹12 (NORMAL)"
        badge_color = "bg-amber-500 text-black" if t.tier_at_creation == "PLUS" else "bg-slate-700 text-white"
        cards_html += f"""
        <div class="p-4 bg-slate-800 rounded-lg border border-slate-700 flex justify-between items-center mb-3">
            <div>
                <span class="px-2 py-1 text-xs font-bold rounded {badge_color}">{bounty_str}</span>
                <span class="ml-2 font-semibold text-white">Doubt #{t.id} — {t.subject}</span>
                <p class="text-xs text-slate-400 mt-1">Student ID: #{t.student_id} (Phone Masked)</p>
            </div>
            <button onclick="claimTicket({t.id})" class="px-4 py-2 bg-emerald-500 hover:bg-emerald-400 text-black font-bold rounded">
                Claim Doubt
            </button>
        </div>
        """

    claimed_html = ""
    for c in my_claimed:
        claimed_html += f"""
        <div class="p-4 bg-indigo-950 rounded-lg border border-indigo-500 mb-4">
            <h4 class="font-bold text-white mb-2">Active Claim: Doubt #{c.id} ({c.subject})</h4>
            <a href="{c.question_image_url}" target="_blank" class="text-emerald-400 underline text-sm block mb-3">View Student Question Photo</a>
            <input id="sol_url_{c.id}" placeholder="Paste Solution Image URL..." class="w-full p-2 rounded bg-slate-900 text-white border border-slate-700 mb-2 text-sm"/>
            <input id="sol_note_{c.id}" placeholder="Short tip / voice note summary..." class="w-full p-2 rounded bg-slate-900 text-white border border-slate-700 mb-2 text-sm"/>
            <button onclick="resolveTicket({c.id})" class="w-full py-2 bg-emerald-500 text-black font-bold rounded">Submit Solution & Claim Bounty</button>
        </div>
        """

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>untwenty5 Mentor Bounty Pool</title>
        <script src="https://cdn.tailwindcss.com"></script>
    </head>
    <body class="bg-slate-950 text-slate-100 p-6 max-w-3xl mx-auto">
        <div class="flex justify-between items-center border-b border-slate-800 pb-4 mb-6">
            <div>
                <h1 class="text-xl font-bold">untwenty5 JEE Mentor Pool ({mentor.subject_specialty})</h1>
                <p class="text-sm text-slate-400">Mentor: {mentor.user.username} | Solved: {mentor.total_solved}</p>
            </div>
            <div class="bg-emerald-950 border border-emerald-500 px-4 py-2 rounded-lg text-right">
                <span class="text-xs text-emerald-300 block">Wallet Balance</span>
                <span class="text-2xl font-bold text-emerald-400">₹{mentor.wallet_balance}</span>
            </div>
        </div>
        <h2 class="text-lg font-semibold mb-3 text-indigo-400">Your Active Claimed Doubts (15-Min SLA)</h2>
        {claimed_html or '<p class="text-sm text-slate-500 mb-6">No active claims. Claim a doubt below!</p>'}
        <h2 class="text-lg font-semibold mb-3 text-emerald-400">Unclaimed {mentor.subject_specialty} Pool</h2>
        {cards_html or '<p class="text-sm text-slate-500">All doubts solved! Waiting for students...</p>'}
        <script>
            async function claimTicket(id) {{
                const res = await fetch(`/api/doubts/${{id}}/claim/`, {{method: 'POST'}});
                const data = await res.json();
                alert(data.error || "Doubt Claimed! You have 15 minutes to upload the solution.");
                location.reload();
            }}
            async function resolveTicket(id) {{
                const url = document.getElementById(`sol_url_${{id}}`).value;
                const note = document.getElementById(`sol_note_${{id}}`).value;
                const res = await fetch(`/api/doubts/${{id}}/resolve/`, {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{solution_image_url: url, solution_notes: note}})
                }});
                const data = await res.json();
                alert(data.error || `Solved! ₹${{data.bounty_credited}} added to your wallet.`);
                location.reload();
            }}
        </script>
    </body>
    </html>
    """
    return HttpResponse(html)
# ---------------------------------------------------------
# 7. end-point for backend to frontend live quota
# ---------------------------------------------------------

@csrf_exempt
def resolve_doubt_ticket(request, ticket_id):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    # 1. Get mentor via custom token (This replaces the request.user check)
    mentor = get_authenticated_mentor(request)
    if not mentor:
        return JsonResponse({"error": "Unauthorized"}, status=401)

    body = json.loads(request.body) if request.content_type == "application/json" else request.POST

    solution_url = body.get("solution_video_url", "").strip()
    notes = body.get("solution_notes", "Solved step-by-step via video.")

    with transaction.atomic():
        ticket = get_object_or_404(
            DoubtTicket.objects.select_for_update(),
            id=ticket_id,
            claimed_by=mentor,
            status="CLAIMED",
        )
        student = StudentSubscription.objects.select_for_update().get(id=ticket.student_id)

        ticket.resolved_at = timezone.now()
        ticket.solution_video_url = solution_url 
        ticket.solution_notes = notes
        ticket.status = "RESOLVED"

        # Safe payout calculation
        payout = Decimal(str(ticket.calculate_payout())) if hasattr(ticket, 'calculate_payout') else Decimal(str(mentor.bounty_per_doubt))
        ticket.bounty_earned = payout
        ticket.save()

        # Credit Mentor Wallet
        mentor.wallet_balance = F("wallet_balance") + payout
        mentor.total_solved = F("total_solved") + 1
        mentor.save()
        mentor.refresh_from_db()

        PayoutTransaction.objects.create(
            mentor=mentor,
            ticket=ticket,
            tx_type="BOUNTY",
            amount=payout,
            notes=f"Solved {ticket.subject} ({ticket.tier_at_creation}) Doubt #{ticket.id}",
        )

        # Unlock Student Queue Slot & Deduct 1 Monthly Quota
        student.active_doubts = max(0, student.active_doubts - 1)
        student.monthly_doubts_remaining = max(0, student.monthly_doubts_remaining - 1)
        student.save()

    # Push solution & updated attributes back to AiSensy (8893209106)
    send_solution_to_student_whatsapp(student, ticket)
    sync_student_to_aisensy(student, current_subject=ticket.subject)

    return JsonResponse({
        "status": "resolved",
        "bounty_credited": float(payout),
        "new_wallet_balance": float(mentor.wallet_balance),
    })

# 8. 
def mentor_board_api(request):
    mentor = get_authenticated_mentor(request)
    if not mentor:
        return JsonResponse({"error": "Unauthorized. Please log in."}, status=401)
    
    # 1. Define the base QuerySet first
    open_qs = DoubtTicket.objects.filter(status="OPEN")
    
    # 2. Apply the filter conditionally
    if mentor.subject_specialty != "ALL":
        open_qs = open_qs.filter(subject=mentor.subject_specialty)
    
    # 3. Apply ordering and fetch values AFTER filtering
    open_tickets = open_qs.order_by("-tier_at_creation", "id").values(
        "id", "subject", "tier_at_creation", "question_image_url", "status"
    )

    # Fetch CLAIMED tickets (You should also filter this by the specific mentor)
    claimed_tickets = DoubtTicket.objects.filter(
        status="CLAIMED", claimed_by=mentor
    ).order_by("-id").values(
        "id", "subject", "tier_at_creation", "question_image_url", "status"
    )
    
    return JsonResponse({
        "mentor": {
            "id": mentor.id,
            "name": mentor.name,
            "subject_specialty": mentor.subject_specialty,
            "total_resolved": mentor.total_solved,
            "bounty_per_doubt": mentor.bounty_per_doubt,
            "total_earnings": mentor.total_solved * mentor.bounty_per_doubt,
        },
        "open_tickets": list(open_tickets),
        "claimed_tickets": list(claimed_tickets),
    })

#9. Video uplaod from mentor
@csrf_exempt
def upload_solution_video(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    
    # Optional: Verify mentor token here using get_authenticated_mentor(request)

    video_file = request.FILES.get('video')
    if not video_file:
        return JsonResponse({"error": "No video file provided"}, status=400)

    # Generate a unique filename
    ext = video_file.name.split('.')[-1] if '.' in video_file.name else 'webm'
    filename = f"solutions/mentor_{uuid.uuid4().hex[:8]}.{ext}"

    # Save to media folder
    saved_path = default_storage.save(filename, video_file)
    
    # Build the full URL to the file
    file_url = request.build_absolute_uri(default_storage.url(saved_path))
    
    return JsonResponse({"video_url": file_url})

# 10. 

def get_authenticated_mentor(request):
    """Helper to extract and verify the Mentor from the Authorization: Bearer <token> header."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header.split("Bearer ")[1].strip()
    try:
        # Token valid for 7 days (604800 seconds)
        payload = signing.loads(token, max_age=604800)
        return MentorProfile.objects.filter(id=payload.get("mentor_id"), is_active=True).first()
    except signing.BadSignature:
        return None

#11.

@csrf_exempt
def mentor_login_api(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    body = json.loads(request.body)
    phone = str(body.get("phone", "")).replace("+", "").strip()
    pin = str(body.get("pin", "")).strip()

    mentor = MentorProfile.objects.filter(phone_number=phone, access_pin=pin, is_active=True).first()
    if not mentor:
        return JsonResponse({"error": "Invalid phone number or PIN."}, status=401)

    token = signing.dumps({"mentor_id": mentor.id, "phone": mentor.phone_number})

    return JsonResponse({
        "token": token,
        "mentor": {
            "id": mentor.id,
            "name": mentor.name,
            "phone": mentor.phone_number,
            "subject_specialty": mentor.subject_specialty,
            "total_solved": mentor.total_solved,
            "bounty_per_doubt": mentor.bounty_per_doubt,
            "total_earnings": mentor.total_solved * mentor.bounty_per_doubt,
            "wallet_balance": float(mentor.wallet_balance),
            "upi_id": mentor.upi_id,
        }
    })

@csrf_exempt
def student_dashboard_api(request):
    # Release any claims older than 15 mins first so the tracker stays accurate
    release_stale_claims()

    phone = request.GET.get("phone", "").replace("+", "").strip()
    student = get_object_or_404(StudentSubscription, phone_number=phone)
    tickets = DoubtTicket.objects.select_related("claimed_by").filter(student=student).order_by("-id")[:20]

    return JsonResponse({
        "student_name": student.student_name,
        "phone": student.phone_number,
        "tier": student.tier,
        "active_doubts": student.active_doubts,
        "max_concurrency": student.max_concurrency,
        "monthly_doubts_remaining": student.monthly_doubts_remaining,
        "expiry_date": student.expiry_date.strftime("%d %b %Y") if student.expiry_date else None,
        "tickets": [
            {
                "id": t.id,
                "subject": t.subject,
                "status": t.status,
                "tier_at_creation": t.tier_at_creation,
                "question_image_url": t.question_image_url,
                "claimed_by_name": t.claimed_by.name if t.claimed_by else None,
                "claimed_at": t.claimed_at.isoformat() if getattr(t, "claimed_at", None) else None,
                "solution_video_url": (
                    getattr(t, "solution_video_url", "") or getattr(t, "solution_image_url", "")
                ),
            }
            for t in tickets
        ]
    })