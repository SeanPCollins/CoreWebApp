from django.contrib import admin
from django.urls import path
from django.conf import settings
from django.contrib.auth.views import LogoutView
from django.views.generic.base import RedirectView
from apps.registry import views

base_url = getattr(settings, 'BASE_APP_URL', '/')
if not base_url.endswith('/'):
    base_url += '/'

admin.site.site_url = '/' + base_url

urlpatterns = [
    path(base_url, views.home_view, name='home'),
    path(f'{base_url}signup/', views.signup, name='signup'),
    path(f'{base_url}description/', views.description_view, name='description'),
    path(f'{base_url}instructions/', views.instructions_view, name='instructions'),
    path(f'{base_url}changelog/', views.changelog_view, name='changelog'),
    path(f'{base_url}contact/', views.contact_view, name='contact'),
    path(f'{base_url}login/', views.CustomLoginView.as_view(), name='login'),
    path(f'{base_url}password-reset/', views.UsernamePasswordResetView.as_view(), name='password_reset'),
    path(f'{base_url}password-reset/done/', views.CustomPasswordResetDoneView.as_view(), name='password_reset_done'),
    path(
        f'{base_url}password-reset/confirm/<uidb64>/<token>/',
        views.CustomPasswordResetConfirmView.as_view(),
        name='password_reset_confirm',
    ),
    path(
        f'{base_url}password-reset/complete/',
        views.CustomPasswordResetCompleteView.as_view(),
        name='password_reset_complete',
    ),
    path(f'{base_url}logout/', LogoutView.as_view(next_page='home'), name='logout'),
    path(f'{base_url}account/', views.account_details_view, name='account_details'),
    path(f'{base_url}search/', views.search_view, name='search'),
    path(
        f'{base_url}search/map/',
        views.chemical_lookup_map_view,
        name='chemical_lookup_map',
    ),
    path(f'{base_url}thanks/', views.thanks_view, name='thanks'),
    path(f'{base_url}activate/<uidb64>/<token>/', views.activate, name='activate'),
    path(f'{base_url}api/check-username/', views.check_username_api, name='check_username_api'),
    path(f'{base_url}admin/', admin.site.urls),
    path(f'{base_url}import/upload/', views.import_upload_view, name='import_upload'),
    path(f'{base_url}import/<uuid:source_import_id>/map/', views.import_map_view, name='import_map',),
    path(f'{base_url}import/<uuid:source_import_id>/status/', views.import_status_view, name='import_status',),
    path(f'{base_url}import/<uuid:source_import_id>/status-data/', views.import_status_data_view, name='import_status_data',),
    path(f'{base_url}import/<uuid:source_import_id>/preview/', views.import_preview_view, name='import_preview',),
    path(f'{base_url}import/<uuid:source_import_id>/commit/', views.import_commit_view, name='import_commit',),
    path(f'{base_url}import/preview/update-row/', views.update_staged_row_cas_ajax, name='import_update_row_ajax'),
    path(f'{base_url}data-gathering/', views.data_gathering_view, name='data_gathering'),
    path(f'{base_url}data-gathering/status/', views.data_gathering_status_view, name='data_gathering_status'),
    path(f'{base_url}structure-curation/', views.structure_curation_view, name='structure_curation'),
    path(
        f'{base_url}structure-curation/<uuid:run_id>/status/',
        views.structure_curation_status_view,
        name='structure_curation_status',
    ),
    path(
        f'{base_url}structure-curation/<uuid:run_id>/download/<str:report_type>/',
        views.structure_curation_download_view,
        name='structure_curation_download',
    ),
    path(
        f'{base_url}extraction/',
        RedirectView.as_view(pattern_name='structure_curation', permanent=False),
        name='extraction_centre',
    ),
]
