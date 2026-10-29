import requests
from django.conf import settings


def sync_student_to_aisensy(student, current_subject="Physics"):
    """
    Updates the student's Custom Attributes in AiSensy so the Flow Builder
    Condition blocks (SubscriptionTier, ActiveDoubts, MonthlyDoubtsRemaining) stay accurate.
    """
    url = "https://backend.aisensy.com/campaign/t1/api/v2"
    payload = {
        "apiKey": settings.AISENSY_API_KEY,
        "campaignName": "student_attribute_sync",
        "destination": str(student.phone_number),
        "userName": student.student_name,
        "userAttributes": {
            "SubscriptionTier": student.tier,
            "ActiveDoubts": str(student.active_doubts),
            "MonthlyDoubtsRemaining": str(student.monthly_doubts_remaining),
            "CurrentSubject": current_subject,
        },
    }
    try:
        response = requests.post(url, json=payload, timeout=8)
        return response.status_code == 200
    except requests.RequestException:
        return False


def send_solution_to_student_whatsapp(student, ticket):
    """
    Sends the mentor's solved image and note directly to the student on WhatsApp via AiSensy Campaign API.
    (Requires an approved Media Template campaign named 'doubt_solution_delivery' in AiSensy).
    """
    url = "https://backend.aisensy.com/campaign/t1/api/v2"
    payload = {
        "apiKey": settings.AISENSY_API_KEY,
        "campaignName": "doubt_solution_delivery",
        "destination": str(student.phone_number),
        "userName": student.student_name,
        "templateParams": [
            student.student_name,
            ticket.subject,
            ticket.claimed_by.user.first_name or "JEE Expert",
            ticket.solution_notes or "Please review the attached step-by-step rough work.",
        ],
        "media": {
            "url": ticket.solution_image_url,
            "filename": f"Solution_Doubt_{ticket.id}.jpg",
        },
        "userAttributes": {
            "SubscriptionTier": student.tier,
            "ActiveDoubts": str(student.active_doubts),
            "MonthlyDoubtsRemaining": str(student.monthly_doubts_remaining),
        },
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except requests.RequestException:
        pass