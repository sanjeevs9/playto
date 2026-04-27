from pathlib import Path

from django.conf import settings
from django.contrib import admin
from django.http import FileResponse, HttpResponse, JsonResponse
from django.urls import include, path, re_path


def healthcheck(_request):
    return JsonResponse({"status": "ok"})


def serve_spa(request, path: str = ""):
    """Serve the React SPA's ``index.html`` for any non-API URL.

    The Vite production build emits an ``index.html`` with asset URLs
    prefixed by ``/static/`` (see ``frontend/vite.config.ts``). WhiteNoise
    serves the assets; this view returns the SPA shell. Cache-Control is
    explicitly ``no-cache`` because the asset filenames inside the HTML
    are hashed — the entry HTML must always be fresh, even though the
    referenced assets can be cached aggressively.

    During local dev the React app runs from the Vite dev server on port
    5173; this view is only used in production / live deploys where the
    SPA and API share an origin.
    """
    index_html = Path(settings.BASE_DIR).parent / "frontend" / "dist" / "index.html"
    if not index_html.exists():
        return HttpResponse(
            "Frontend not built. Run `npm run build` in frontend/.",
            status=503,
            content_type="text/plain",
        )
    response = FileResponse(open(index_html, "rb"), content_type="text/html")
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthz", healthcheck),
    path("api/v1/", include("payouts.urls")),
    path("api/v1/", include("merchants.urls")),
    # SPA catch-all — must be LAST. Anything that doesn't match the API or
    # admin gets the SPA shell. WhiteNoise short-circuits ``/static/...``
    # requests before this view runs (it sits ahead of the URL resolver in
    # MIDDLEWARE), so the catch-all is safe.
    re_path(r"^.*$", serve_spa, name="spa"),
]
