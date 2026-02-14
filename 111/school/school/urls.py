from django.contrib import admin
from django.contrib import admin
from django.urls import path, re_path
from Schoolapp import views
from Schoolapp import test_views
from Schoolapp import mobile_api_views
from Schoolapp.health_views import health_check, status_view
from django.conf import settings
from django.conf.urls.static import static
from django.http import FileResponse, Http404
from django.views.static import serve
import os







urlpatterns = [
    path('_test/simple/', test_views.test_simple, name='test_simple'),
    path('_test/template/', test_views.test_template, name='test_template'),
    path('_test/db/', test_views.test_db, name='test_db'),
    path('health/', health_check, name='health'),
    path('status/', status_view, name='status'),
      path('admin/', admin.site.urls),
  # Show login page at root so opening the app lands on the login screen
  path('', views.login_view, name='home'),
    path('api/dashboard/stats/', views.dashboard_stats, name='api_dashboard_stats'),
    path('dashboard/', views.dashboard, name='dashboard'),
    path('etudiants/', views.etudiants_view, name='etudiants'),
  path('etudiants/export/xlsx/', views.etudiants_export_xlsx, name='etudiants_export_xlsx'),
  path('etudiants/export/pdf/', views.etudiants_export_pdf, name='etudiants_export_pdf'),
    path('etudiants/add/', views.add_etudiant, name='etudiants_add'),
    path('etudiants/edit/', views.edit_etudiant, name='etudiants_edit'),
    path('etudiants/delete/', views.delete_etudiant, name='etudiants_delete'),
    path('formations/', views.formations_view, name='formations'),
    path('formations/add/', views.add_formation, name='formations_add'),
  path('formations/<int:pk>/pdf/', views.formation_pdf, name='formation_pdf'),
    path('formations/edit/', views.edit_formation, name='formations_edit'),
    path('formations/delete/', views.delete_formation, name='formations_delete'),
    path('formateurs/', views.formateurs_view, name='formateurs'),
    path('planning/', views.planning_view, name='planning'),
    path('api/planning/events/', views.planning_events_api, name='api_planning_events'),
    path('api/planning/events/add/', views.add_planning_event, name='api_planning_events_add'),
  path('api/planning/events/edit/', views.edit_planning_event, name='api_planning_events_edit'),
    path('api/planning/events/delete/', views.delete_planning_event, name='api_planning_events_delete'),
    path('api/planning/day/', views.planning_day_api, name='api_planning_day'),
    path('api/planning/room/', views.planning_room_api, name='api_planning_room'),
    path('examens/', views.examens_view, name='examens'),
    path('inscriptions/', views.inscriptions_view, name='inscriptions'),
    path('sw-paiements.js', views.sw_paiements, name='sw_paiements'),
    path('inscriptions/add/', views.add_inscription, name='inscriptions_add'),
  path('inscriptions/delete/', views.inscriptions_delete, name='inscriptions_delete'),
  path('inscriptions/edit/', views.edit_inscription, name='inscriptions_edit'),
    path('finances/', views.finances_view, name='finances'),
  # Per-school payments page and bulk-add endpoint
  path('finances/ecoles/', views.ecole_paiements_view, name='ecole_paiements'),
  path('finances/ecoles/export_pdf/', views.ecole_paiements_export_pdf, name='ecoles_export_pdf'),
  path('finances/ecoles/add_bulk/', views.add_ecole_paiements_bulk, name='add_ecole_paiements_bulk'),
  path('finances/ecoles/versement_add/', views.add_ecole_versement, name='add_ecole_versement'),
  path('finances/ecoles/batch_detail/', views.ecole_versement_detail, name='ecole_versement_detail'),
  path('finances/ecoles/batch_delete/', views.delete_versement_batch, name='ecole_versement_delete'),
  path('api/invest-totals/', views.api_invest_totals, name='api_invest_totals'),
  # Charges CRUD
  path('finances/charges/', views.charges_list, name='charges'),
  path('finances/charges/add/', views.charge_create, name='charges_add'),
  path('finances/charges/parse-ai/', views.charge_parse_ai, name='charges_parse_ai'),
  path('finances/charges/chat-ai/', views.charge_ai_chat, name='charges_chat_ai'),
  path('finances/charges/<int:pk>/edit/', views.charge_edit, name='charges_edit'),
  path('finances/charges/<int:pk>/delete/', views.charge_delete, name='charges_delete'),
  # Mobile API for Charges
  path('api/mobile/charges/all/', views.api_charges_all, name='api_charges_all'),
  path('api/mobile/charges/', views.api_charges_list, name='api_charges_list'),
  path('api/mobile/charges/stats/', views.api_charges_stats, name='api_charges_stats'),
  path('api/mobile/charges/types/', views.api_charges_types, name='api_charges_types'),
  path('api/mobile/monthly-trends/', views.api_monthly_trends, name='api_monthly_trends'),
  path('api/mobile/charges/create/', views.api_charge_create, name='api_charge_create'),
  path('api/mobile/charges/<int:pk>/', views.api_charge_detail, name='api_charge_detail'),
  path('api/mobile/charges/<int:pk>/update/', views.api_charge_update, name='api_charge_update'),
  path('api/mobile/charges/<int:pk>/delete/', views.api_charge_delete, name='api_charge_delete'),
  path('api/mobile/upload-temp/', views.api_upload_temp, name='api_upload_temp'),

  # Mobile API for Finances
  path('api/mobile/finances/summary/', views.api_finances_summary, name='api_finances_summary'),
  path('api/mobile/paiements/', views.api_paiements_list, name='api_paiements_list'),
  path('api/mobile/paiements/create/', views.api_paiement_create, name='api_paiement_create'),
  path('api/mobile/etudiants/', views.api_etudiants_simple, name='api_etudiants_simple'),

  path('api/mobile/ecoles/', views.api_mobile_ecoles_list, name='api_mobile_ecoles_list'),
  path('api/mobile/ecoles/detail/', views.api_mobile_ecole_detail, name='api_mobile_ecole_detail'),
    path('paiements/', views.paiements_view, name='paiements'),
    path('paiements/add/', views.add_paiement, name='paiements_add'),
  path('paiements/edit/', views.edit_paiement, name='paiements_edit'),
  path('paiements/delete/', views.delete_paiement, name='paiements_delete'),
    path('paiements/<int:pk>/receipt/', views.paiement_receipt, name='paiement_receipt'),
  path('paiements/<int:pk>/receipt/pdf/', views.paiement_receipt_pdf, name='paiement_receipt_pdf'),
    # Etudiant verification APIs
    path('etudiants/<int:pk>/json/', views.etudiant_detail_json, name='etudiant_detail_json'),
    path('etudiants/<int:pk>/upload-doc/', views.etudiant_upload_doc, name='etudiant_upload_doc'),
    path('etudiants/<int:pk>/update-step/', views.etudiant_update_step, name='etudiant_update_step'),
    path('etudiants/<int:pk>/toggle-paiement/', views.toggle_etudiant_paiement, name='etudiant_toggle_paiement'),
    path('rapports/', views.rapports_view, name='rapports'),
    path('enseignants/', views.enseignants_view, name='enseignants'),
    path('enseignants/add/', views.add_enseignant, name='enseignants_add'),
    path('enseignants/edit/', views.edit_enseignant, name='enseignants_edit'),
    path('enseignants/delete/', views.delete_enseignant, name='enseignants_delete'),

  # Settings page (placeholder)
  path('parametres/', views.parametres_view, name='parametres'),
  path('parametres/update-school/', views.parametres_update_school, name='parametres_update_school'),
  path('parametres/update-profile/', views.parametres_update_profile, name='parametres_update_profile'),
  path('parametres/set-database/', views.parametres_set_database, name='parametres_set_database'),
  path('toggle-db/', views.toggle_active_db, name='toggle_active_db'),

  # User management endpoints used by parametres
  path('users/upsert/', views.users_upsert, name='users_upsert'),
  path('users/delete/', views.users_delete, name='users_delete'),

  # Salles (rooms) AJAX endpoints for planning page
  path('salles/list/', views.salle_list, name='salle_list'),
  path('salles/upsert/', views.salle_upsert, name='salle_upsert'),
  path('salles/delete/', views.salle_delete, name='salle_delete'),
  # Groupes AJAX endpoints
  path('groupes/add/', views.groupe_add, name='groupe_add'),
  path('groupes/delete/', views.groupe_delete, name='groupe_delete'),

  # Authentication
  path('login/', views.login_view, name='login'),
  path('logout/', views.logout_view, name='logout'),

    # Reglements enseignants
    path('reglements/enseignants/', views.reglements_enseignants_view, name='reglements_enseignants'),
    path('reglements/enseignants/add/', views.add_reglement_enseignant, name='reglements_enseignants_add'),
    path('reglements/enseignants/delete/', views.delete_reglement_enseignant, name='reglements_enseignants_delete'),
  path('reglements/enseignants/edit/', views.edit_reglement_enseignant, name='reglements_enseignants_edit'),
  # Reglements fournisseurs
  path('reglements/fournisseurs/', views.reglements_fournisseurs_view, name='reglements_fournisseurs'),
  path('api/fournisseur/create/', views.api_fournisseur_create, name='api_fournisseur_create'),
  path('api/achat/create/', views.api_achat_create, name='api_achat_create'),
  path('api/reglement_fournisseur/create/', views.api_reglement_fournisseur_create, name='api_reglement_fournisseur_create'),

  # Presence pages
  path('presence/', views.PresenceListView.as_view(), name='presence_list'),
  # backward-compatible short name used in templates
  path('presence/', views.PresenceListView.as_view(), name='presence'),
  path('presence/add/', views.PresenceCreateView.as_view(), name='presence_add'),
  path('presence/clock-in/', views.presence_clock_in, name='presence_clock_in'),
  path('presence/<int:pk>/', views.PresenceDetailView.as_view(), name='presence_detail'),
  path('presence/<int:pk>/edit/', views.PresenceUpdateView.as_view(), name='presence_edit'),
  path('presence/<int:pk>/delete/', views.PresenceDeleteView.as_view(), name='presence_delete'),
  path('presence/<int:pk>/delete-ajax/', views.presence_delete_ajax, name='presence_delete_ajax'),
  path('api/etudiants/search/', views.api_search_etudiants, name='api_etudiants_search'),

  # Student Mobile Platform Routes
  path('student/dashboard/', views.student_dashboard, name='student_dashboard'),
  path('student/profile/edit/', views.student_profile_edit, name='student_profile_edit'),
  path('student/inscriptions/', views.student_inscriptions_detailed, name='student_inscriptions'),
  path('student/payments/', views.student_payments_detailed, name='student_payments'),
  path('student/planning/', views.student_planning, name='student_planning'),
  path('student/formations/', views.student_formations_available, name='student_formations'),

  # Student Mobile API
  path('api/mobile/student/login/', views.api_student_login, name='api_student_login'),
  path('api/mobile/student/profile/', views.api_student_profile, name='api_student_profile'),
  path('api/mobile/student/profile/update/', views.api_student_profile_update, name='api_student_profile_update'),
  path('api/mobile/student/formations/', views.api_student_formations, name='api_student_formations'),
  path('api/mobile/student/inscriptions/', views.api_student_inscriptions, name='api_student_inscriptions'),
  path('api/mobile/student/inscriptions/create/', views.api_student_inscription_create, name='api_student_inscription_create'),
  path('api/mobile/student/payments/', views.api_student_payments, name='api_student_payments'),
  
  # Student Mobile App
  path('m/student/', views.mobile_student_app, name='mobile_student_app'),
  
  # New Mobile APIs
  path('api/mobile/student/signup/', views.api_student_signup, name='api_student_signup'),
  path('api/mobile/student/upload-docs/', views.api_student_upload_docs, name='api_student_upload_docs'),
  
  # Enhanced Mobile API v2 - For Flutter App
  path('api/mobile/v2/student/login/', mobile_api_views.api_mobile_student_login, name='api_mobile_v2_student_login'),
  path('api/mobile/v2/student/dashboard/', mobile_api_views.api_mobile_student_dashboard, name='api_mobile_v2_student_dashboard'),
  path('api/mobile/v2/student/formations/', mobile_api_views.api_mobile_student_formations, name='api_mobile_v2_student_formations'),
  path('api/mobile/v2/student/payments/', mobile_api_views.api_mobile_student_payments, name='api_mobile_v2_student_payments'),
  path('api/mobile/v2/student/profile/update/', mobile_api_views.api_mobile_student_profile_update, name='api_mobile_v2_student_profile_update'),
  path('api/mobile/v2/scan-id-card/', mobile_api_views.api_mobile_scan_id_card, name='api_mobile_v2_scan_id_card'),
  path('api/mobile/v2/student/signup/', mobile_api_views.api_mobile_v2_student_signup, name='api_mobile_v2_student_signup'),
  path('api/mobile/v2/student/enroll/', mobile_api_views.api_mobile_v2_student_enroll, name='api_mobile_v2_student_enroll'),

  # Admin Approval for Mobile Enrollments
  path('admin/pending-enrollments/', views.admin_pending_enrollments, name='admin_pending_enrollments'),
  path('admin/approve-enrollment/', views.admin_approve_enrollment, name='admin_approve_enrollment'),


]

# Serve media files in production (using Django's static serve view)
# This works even when DEBUG=False
urlpatterns += [
    re_path(r'^media/(?P<path>.*)$', serve, {'document_root': settings.MEDIA_ROOT}),
]
