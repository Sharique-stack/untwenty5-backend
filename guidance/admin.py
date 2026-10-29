from django.contrib import admin
from django.urls import path
from guidance import views
from .models import StudentSubscription, MentorProfile, DoubtTicket, PayoutTransaction


urlpatterns = [
    path("admin/", admin.site.urls),
    path("webhooks/razorpay/", views.razorpay_webhook, name="razorpay_webhook"),
    path("api/doubts/create/", views.aisensy_create_doubt, name="aisensy_create_doubt"),
    path("api/doubts/<int:ticket_id>/claim/", views.claim_doubt_ticket, name="claim_doubt"),
    path("api/doubts/<int:ticket_id>/resolve/", views.resolve_doubt_ticket, name="resolve_doubt"),
    path("api/cron/daily/", views.daily_lifecycle_cron, name="daily_cron"),
    path("mentor/board/", views.mentor_bounty_board, name="mentor_board"),
]

@admin.register(StudentSubscription)
class StudentSubscriptionAdmin(admin.ModelAdmin):
    list_display = ("student_name", "phone_number", "tier", "active_doubts", "monthly_doubts_remaining", "expiry_date")
    search_fields = ("phone_number", "student_name")
    list_filter = ("tier",)

@admin.register(MentorProfile)
class MentorProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "subject_specialty", "wallet_balance", "total_solved", "upi_id", "is_active")
    list_filter = ("subject_specialty", "is_active")

@admin.register(DoubtTicket)
class DoubtTicketAdmin(admin.ModelAdmin):
    list_display = ("id", "subject", "tier_at_creation", "status", "claimed_by", "bounty_earned", "created_at")
    list_filter = ("status", "subject", "tier_at_creation")

@admin.register(PayoutTransaction)
class PayoutTransactionAdmin(admin.ModelAdmin):
    list_display = ("mentor", "tx_type", "amount", "ticket", "created_at", "notes")
    list_filter = ("tx_type",)