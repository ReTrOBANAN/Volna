from django.urls import path
from django.contrib.auth.views import LoginView, LogoutView

from . import views
from .forms import RussianAuthenticationForm

app_name = "newsroom"

urlpatterns = [
    path("login/", LoginView.as_view(template_name="registration/login.html", authentication_form=RussianAuthenticationForm, redirect_authenticated_user=True), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("", views.dashboard, name="dashboard"),
    path("radio/shifts/", views.shifts, name="shifts"),
    path("radio/shifts/day/", views.shift_day_schedule, name="shift_day_schedule"),
    path("radio/shifts/new/", views.shift_create, name="shift_create"),
    path("radio/shifts/<int:pk>/edit/", views.shift_create, name="shift_edit"),
    path("radio/shifts/<int:pk>/delete/", views.shift_delete, name="shift_delete"),
    path("studios/", views.bookings, name="bookings"),
    path("studios/day/", views.booking_day_schedule, name="booking_day_schedule"),
    path("studios/statuses/", views.booking_statuses, name="booking_statuses"),
    path("studios/new/", views.booking_create, name="booking_create"),
    path("studios/<int:pk>/", views.booking_detail, name="booking_detail"),
    path("studios/<int:pk>/edit/", views.booking_create, name="booking_edit"),
    path("studios/<int:pk>/action/", views.booking_action, name="booking_action"),
    path("events/", views.events, name="events"),
    path("events/new/", views.event_create, name="event_create"),
    path("events/<int:pk>/", views.event_detail, name="event_detail"),
    path("events/<int:pk>/edit/", views.event_create, name="event_edit"),
    path("events/<int:pk>/action/", views.event_action, name="event_action"),
    path("files/", views.files, name="files"),
    path("files/upload/", views.file_upload, name="file_upload"),
    path("files/<int:pk>/download/", views.file_download, name="file_download"),
    path("files/<int:pk>/delete/", views.file_delete, name="file_delete"),
    path("proposals/", views.proposals, name="proposals"),
    path("proposals/new/", views.proposal_create, name="proposal_create"),
    path("proposals/<int:pk>/", views.proposal_detail, name="proposal_detail"),
    path("proposals/<int:pk>/review/", views.proposal_review, name="proposal_review"),
    path("employees/", views.employees, name="employees"),
    path("employees/new/", views.employee_create, name="employee_create"),
    path("employees/<int:pk>/edit/", views.employee_create, name="employee_edit"),
    path("profile/", views.profile, name="profile"),
    path("profile/password/", views.password_change, name="password_change"),
    path("notifications/", views.notifications, name="notifications"),
    path("notifications/poll/", views.notification_poll, name="notification_poll"),
    path("notifications/<int:pk>/read/", views.notification_read, name="notification_read"),
    path("radio/news/", views.news, name="news"),
]
