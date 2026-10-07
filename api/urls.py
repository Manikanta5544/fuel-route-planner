from django.urls import path

from api import views

urlpatterns = [
    path("api/v1/route", views.route),
    path("api/v1/route/map", views.route_map),
    path("healthz", views.healthz),
    path("readyz", views.readyz),
]
