from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path


def healthcheck(_request):
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthz", healthcheck),
    path("api/v1/", include("payouts.urls")),
    path("api/v1/", include("merchants.urls")),
]
