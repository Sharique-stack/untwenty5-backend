from django.contrib import admin
from django.urls import path
from django.conf import settings
from django.conf.urls.static import static
from guidance import views

urlpatterns = [
    path("admin/", admin.site.urls),
    
    # Student APIs
    path("api/student/dashboard/", views.student_dashboard_api, name="student_dashboard_api"),
    path("api/doubts/create/", views.aisensy_create_doubt, name="aisensy_create_doubt"),
    
    # Mentor APIs
    path("api/mentor/login/", views.mentor_login_api, name="mentor_login_api"),
    path("api/mentor/board/", views.mentor_board_api, name="mentor_board_api"),
    path("api/doubts/<int:ticket_id>/claim/", views.claim_doubt_ticket, name="claim_doubt"),
    path("api/doubts/<int:ticket_id>/resolve/", views.resolve_doubt_ticket, name="resolve_doubt"),
    path("api/upload/video/", views.upload_solution_video, name="upload_video"),
    
    # Webhooks & Cron
    path("webhooks/razorpay/", views.razorpay_webhook, name="razorpay_webhook"),
    path("api/cron/daily/", views.daily_lifecycle_cron, name="daily_cron"),
]

# Serve media files (like recorded solution videos) during local development
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)