from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from datetime import timedelta


class StudentSubscription(models.Model):
    TIER_CHOICES = [
        ("NORMAL", "Normal"),
        ("PLUS", "Plus"),
        ("EXPIRED", "Expired"),
    ]

    phone_number = models.CharField(max_length=15, unique=True, db_index=True)
    student_name = models.CharField(max_length=100, default="JEE Aspirant")
    tier = models.CharField(max_length=10, choices=TIER_CHOICES, default="NORMAL")
    duration_days = models.IntegerField(default=90)  # 90 (3M) or 365 (12M)

    active_doubts = models.IntegerField(default=0)
    monthly_doubts_remaining = models.IntegerField(default=25)
    free_trial_used = models.BooleanField(default=False)

    start_date = models.DateTimeField(auto_now_add=True)
    next_quota_reset_date = models.DateTimeField()
    expiry_date = models.DateTimeField()

    @property
    def max_concurrency(self):
        return 2 if self.tier == "PLUS" else 1

    @property
    def base_monthly_quota(self):
        return 45 if self.tier == "PLUS" else 25

    def __str__(self):
        return f"{self.student_name} ({self.phone_number}) - {self.tier}"


class MentorProfile(models.Model):
    SUBJECT_CHOICES = [
        ("Physics", "Physics"),
        ("Chemistry", "Chemistry"),
        ("Mathematics", "Mathematics"),
        ("ALL", "All Subjects (Admin/Lead)"),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="mentor_profile")
    name = models.CharField(max_length=100)
    phone_number = models.CharField(max_length=15)
    access_pin = models.CharField(max_length=20, default="1234", help_text="4-6 digit PIN or password for login")
    subject_specialty = models.CharField(max_length=20, choices=SUBJECT_CHOICES)
    upi_id = models.CharField(max_length=100, blank=True, null=True)
    wallet_balance = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    bounty_per_doubt = models.PositiveIntegerField(default=30, help_text="Payout in INR per resolved doubt")
    total_solved = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.user.get_full_name() or self.user.username} ({self.primary_subject}) - ₹{self.wallet_balance}"


class DoubtTicket(models.Model):
    STATUS_CHOICES = [
        ("OPEN", "Open in Pool"),
        ("CLAIMED", "Claimed by Mentor"),
        ("RESOLVED", "Resolved & Paid"),
        ("DISPUTED", "Student Still Confused"),
    ]
    SUBJECT_CHOICES = [
        ("Physics", "Physics"),
        ("Chemistry", "Chemistry"),
        ("Mathematics", "Mathematics"),
    ]

    student = models.ForeignKey(StudentSubscription, on_delete=models.CASCADE, related_name="doubts")
    subject = models.CharField(max_length=20, choices=SUBJECT_CHOICES)
    tier_at_creation = models.CharField(max_length=10, default="NORMAL")
    question_image_url = models.URLField(max_length=500, blank=True, null=True)
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default="OPEN", db_index=True)

    claimed_by = models.ForeignKey(
        MentorProfile, on_delete=models.SET_NULL, null=True, blank=True, related_name="claimed_doubts"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    solution_image_url = models.URLField(max_length=500, blank=True, null=True)
    solution_notes = models.TextField(blank=True, null=True)
    bounty_earned = models.DecimalField(max_digits=6, decimal_places=2, default=0.00)

    def calculate_payout(self):
        """
        Calculates mentor bounty:
        PLUS: ₹15 Base + ₹5 Speed Bonus (<15 mins) = ₹20
        NORMAL: ₹10 Base + ₹2 Speed Bonus (<15 mins) = ₹12
        """
        base = 15.00 if self.tier_at_creation == "PLUS" else 10.00
        bonus = 0.00
        if self.claimed_at and self.resolved_at:
            minutes_taken = (self.resolved_at - self.created_at).total_seconds() / 60.0
            if minutes_taken <= 15:
                bonus = 5.00 if self.tier_at_creation == "PLUS" else 2.00
        return base + bonus

    def __str__(self):
        return f"Doubt #{self.id} [{self.subject} - {self.tier_at_creation}] ({self.status})"


class PayoutTransaction(models.Model):
    TX_TYPES = [
        ("BOUNTY", "Doubt Solved Bounty"),
        ("PENALTY", "Claim Abandonment Penalty"),
        ("WITHDRAWAL", "Weekly UPI Payout"),
    ]
    mentor = models.ForeignKey(MentorProfile, on_delete=models.CASCADE, related_name="transactions")
    ticket = models.ForeignKey(DoubtTicket, on_delete=models.SET_NULL, null=True, blank=True)
    tx_type = models.CharField(max_length=15, choices=TX_TYPES)
    amount = models.DecimalField(max_digits=8, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)
    notes = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return f"{self.mentor.user.username} | {self.tx_type} | ₹{self.amount}"