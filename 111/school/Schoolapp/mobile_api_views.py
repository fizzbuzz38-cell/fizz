"""
Mobile API Views for Student Platform
Enhanced endpoints for Flutter mobile application
"""

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.db.models import Sum, Q, Count, Avg
from django.conf import settings
from decimal import Decimal
from datetime import datetime, timedelta
import json

from .models import (
    Etudiant, Formation, Inscription, Paiement,
    CalendarEvent, Groupe, Enseignant, Module
)
from .utils import calculate_balances


@csrf_exempt
@require_http_methods(["POST"])
def api_mobile_student_login(request):
    """
    Enhanced student login endpoint for mobile app.
    POST /api/mobile/v2/student/login/
    Body: { "student_id": "123" } or { "email": "student@example.com" }
    """
    try:
        data = json.loads(request.body)
        student_id = data.get('student_id')
        email = data.get('email')
        
        if not student_id and not email:
            return JsonResponse({
                'success': False,
                'error': 'student_id ou email requis'
            }, status=400)
        
        # Find student
        etudiant = None
        if student_id:
            try:
                etudiant = Etudiant.objects.get(pk=int(student_id))
            except (Etudiant.DoesNotExist, ValueError):
                pass
        
        if not etudiant and email:
            try:
                etudiant = Etudiant.objects.get(email=email)
            except Etudiant.DoesNotExist:
                pass
        
        if not etudiant:
            return JsonResponse({
                'success': False,
                'error': 'Étudiant non trouvé'
            }, status=404)
        
        # Build response with complete student data
        photo_url = ''
        if etudiant.photo:
            if str(etudiant.photo).startswith('http'):
                photo_url = etudiant.photo
            else:
                photo_url = request.build_absolute_uri(settings.MEDIA_URL + str(etudiant.photo))
        
        # Get formation info
        formation_nom = ''
        groupe_nom = ''
        if etudiant.formation:
            formation_nom = etudiant.formation.nom
        if etudiant.groupe:
            groupe_nom = etudiant.groupe.nom
        
        return JsonResponse({
            'success': True,
            'student': {
                'id': str(etudiant.id),
                'nom': etudiant.nom or '',
                'prenom': etudiant.prenom or '',
                'email': etudiant.email or '',
                'telephone': etudiant.telephone or '',
                'mobile': etudiant.mobile or '',
                'photo': photo_url,
                'formation_nom': formation_nom,
                'groupe_nom': groupe_nom,
                'statut': etudiant.statut or 'actif',
                'date_naissance': etudiant.date_naissance.isoformat() if etudiant.date_naissance else None,
                'lieu_naissance': etudiant.lieu_naissance or '',
                'nationalite': etudiant.nationalite or '',
                'adresse': etudiant.adresse or '',
                'niveau_etude': etudiant.niveau_etude or '',
                'situation_professionnelle': etudiant.situation_professionnelle or '',
                'nin': etudiant.nin or '',
                'date_inscription': etudiant.date_inscription.isoformat() if etudiant.date_inscription else None,
            }
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': f'Erreur serveur: {str(e)}'
        }, status=500)


@csrf_exempt
@require_http_methods(["GET"])
def api_mobile_student_dashboard(request):
    """
    Get comprehensive dashboard data for student.
    GET /api/mobile/v2/student/dashboard/?student_id=123
    """
    try:
        student_id = request.GET.get('student_id')
        if not student_id:
            return JsonResponse({
                'success': False,
                'error': 'student_id requis'
            }, status=400)
        
        try:
            etudiant = Etudiant.objects.get(pk=int(student_id))
        except (Etudiant.DoesNotExist, ValueError):
            return JsonResponse({
                'success': False,
                'error': 'Étudiant non trouvé'
            }, status=404)
        
        # Get inscriptions
        inscriptions = Inscription.objects.filter(etudiant=etudiant).select_related('formation')
        total_formations = inscriptions.count()
        
        # Calculate average progress
        avg_progress = inscriptions.aggregate(avg=Avg('progress_percent'))['avg'] or 0
        
        # Get payments summary
        paiements = Paiement.objects.filter(etudiant=etudiant)
        total_paid = float(paiements.aggregate(total=Sum('montant'))['total'] or 0)
        
        # Calculate total due and remaining
        total_due = 0
        for ins in inscriptions:
            if ins.prix_total:
                total_due += float(ins.prix_total)
            elif ins.formation and ins.formation.prix_etudiant:
                total_due += float(ins.formation.prix_etudiant)
        
        remaining = max(0, total_due - total_paid)
        payment_progress = min(100, (total_paid / total_due * 100)) if total_due > 0 else 100
        
        # Get upcoming events
        now = datetime.now()
        upcoming_events = CalendarEvent.objects.filter(
            Q(formation_id__in=inscriptions.values_list('formation_id', flat=True)) |
            Q(groupe__in=inscriptions.values_list('groupe_id', flat=True))
        ).filter(
            start_datetime__gte=now
        ).order_by('start_datetime')[:5]
        
        events_list = []
        for event in upcoming_events:
            events_list.append({
                'id': event.id,
                'titre': event.titre or '',
                'description': event.description or '',
                'start_datetime': event.start_datetime.isoformat() if event.start_datetime else None,
                'end_datetime': event.end_datetime.isoformat() if event.end_datetime else None,
                'is_online': event.is_online or False,
                'formation_name': event.formation_name or '',
                'formateur_name': event.formateur_name or '',
            })
        
        # Get recent news/announcements (last 5 events from past)
        recent_news = CalendarEvent.objects.filter(
            Q(formation_id__in=inscriptions.values_list('formation_id', flat=True)) |
            Q(groupe__in=inscriptions.values_list('groupe_id', flat=True))
        ).filter(
            start_datetime__lt=now
        ).order_by('-start_datetime')[:5]
        
        news_list = []
        for news in recent_news:
            news_list.append({
                'id': news.id,
                'titre': news.titre or '',
                'description': news.description or '',
                'date': news.start_datetime.isoformat() if news.start_datetime else None,
                'formation_name': news.formation_name or '',
            })
        
        # Count overdue payments
        overdue_payments = 0
        for ins in inscriptions:
            if ins.prix_total:
                ins_total = float(ins.prix_total)
                ins_paid = float(paiements.filter(inscription=ins).aggregate(total=Sum('montant'))['total'] or 0)
                if ins_paid < ins_total:
                    overdue_payments += 1
        
        return JsonResponse({
            'success': True,
            'stats': {
                'total_formations': total_formations,
                'average_progress': round(avg_progress, 1),
                'total_paid': round(total_paid, 2),
                'total_due': round(total_due, 2),
                'remaining': round(remaining, 2),
                'payment_progress': round(payment_progress, 1),
                'upcoming_events': len(events_list),
                'overdue_payments': overdue_payments,
            },
            'news': news_list,
            'events': events_list,
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': f'Erreur serveur: {str(e)}'
        }, status=500)


@csrf_exempt
@require_http_methods(["GET"])
def api_mobile_student_formations_list(request):
    """
    Get student's enrolled formations with detailed progress.
    GET /api/mobile/v2/student/formations/?student_id=123
    """
    try:
        student_id = request.GET.get('student_id')
        if not student_id:
            return JsonResponse({
                'success': False,
                'error': 'student_id requis'
            }, status=400)
        
        try:
            etudiant = Etudiant.objects.get(pk=int(student_id))
        except (Etudiant.DoesNotExist, ValueError):
            return JsonResponse({
                'success': False,
                'error': 'Étudiant non trouvé'
            }, status=404)
        
        # Get inscriptions with related data
        inscriptions = Inscription.objects.filter(
            etudiant=etudiant
        ).select_related('formation', 'groupe').order_by('-date_inscription')
        
        formations_list = []
        for ins in inscriptions:
            if not ins.formation:
                continue
            
            formation = ins.formation
            
            # Build photo URL
            photo_url = ''
            if formation.photo:
                if str(formation.photo).startswith('http'):
                    photo_url = formation.photo
                else:
                    photo_url = request.build_absolute_uri(settings.MEDIA_URL + str(formation.photo))
            
            # Get instructor
            instructor = None
            try:
                ens = Enseignant.objects.filter(formation=formation, is_active=True).first()
                if ens:
                    ens_photo = ''
                    if ens.photo:
                        if str(ens.photo).startswith('http'):
                            ens_photo = ens.photo
                        else:
                            ens_photo = request.build_absolute_uri(settings.MEDIA_URL + str(ens.photo))
                    instructor = {
                        'name': f"{ens.prenom} {ens.nom}",
                        'photo': ens_photo,
                        'specialite': ens.specialite or '',
                    }
            except:
                pass
            
            # Calculate payment info
            prix_total = float(ins.prix_total or formation.prix_etudiant or 0)
            paid = float(Paiement.objects.filter(
                etudiant=etudiant,
                Q(inscription=ins) | Q(formation=formation)
            ).aggregate(total=Sum('montant'))['total'] or 0)
            remaining = max(0, prix_total - paid)
            payment_progress = min(100, (paid / prix_total * 100)) if prix_total > 0 else 100
            
            # Get modules count
            modules_count = Module.objects.filter(formation=formation).count()
            
            formations_list.append({
                'id': formation.id,
                'nom': formation.nom or '',
                'description': formation.contenu or '',
                'photo': photo_url,
                'categorie': formation.categorie or '',
                'niveau': formation.niveau or '',
                'duree': formation.duree or '',
                'prix': prix_total,
                'paid': paid,
                'remaining': remaining,
                'payment_progress': round(payment_progress, 1),
                'progress_percent': float(ins.progress_percent or 0),
                'groupe': ins.groupe.nom if ins.groupe else None,
                'session': ins.session or '',
                'date_inscription': ins.date_inscription.isoformat() if ins.date_inscription else None,
                'statut': ins.statut or 'actif',
                'instructor': instructor,
                'modules_count': modules_count,
            })
        
        return JsonResponse({
            'success': True,
            'formations': formations_list,
            'total': len(formations_list)
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': f'Erreur serveur: {str(e)}'
        }, status=500)


@csrf_exempt
@require_http_methods(["GET"])
def api_mobile_student_payments_list(request):
    """
    Get student's payment history with enhanced details.
    GET /api/mobile/v2/student/payments/?student_id=123
    """
    try:
        student_id = request.GET.get('student_id')
        if not student_id:
            return JsonResponse({
                'success': False,
                'error': 'student_id requis'
            }, status=400)
        
        try:
            etudiant = Etudiant.objects.get(pk=int(student_id))
        except (Etudiant.DoesNotExist, ValueError):
            return JsonResponse({
                'success': False,
                'error': 'Étudiant non trouvé'
            }, status=404)
        
        # Get all payments
        paiements = Paiement.objects.filter(
            etudiant=etudiant
        ).select_related('formation', 'inscription').order_by('-date_paiement', '-created_at')
        
        # Calculate summary
        total_paid = float(paiements.aggregate(total=Sum('montant'))['total'] or 0)
        
        # Get total due from inscriptions
        inscriptions = Inscription.objects.filter(etudiant=etudiant).select_related('formation')
        total_due = 0
        for ins in inscriptions:
            if ins.prix_total:
                total_due += float(ins.prix_total)
            elif ins.formation and ins.formation.prix_etudiant:
                total_due += float(ins.formation.prix_etudiant)
        
        remaining = max(0, total_due - total_paid)
        progress = min(100, (total_paid / total_due * 100)) if total_due > 0 else 100
        
        # Count overdue
        overdue_count = 0
        for ins in inscriptions:
            ins_total = float(ins.prix_total or ins.formation.prix_etudiant or 0) if ins.formation else 0
            ins_paid = float(paiements.filter(
                Q(inscription=ins) | Q(formation=ins.formation)
            ).aggregate(total=Sum('montant'))['total'] or 0)
            if ins_paid < ins_total:
                overdue_count += 1
        
        # Build payments list
        payments_list = []
        for p in paiements:
            formation_name = ''
            if p.formation:
                formation_name = p.formation.nom
            elif p.inscription and p.inscription.formation:
                formation_name = p.inscription.formation.nom
            
            # Determine status
            status = 'paid'
            if p.statut and 'pending' in p.statut.lower():
                status = 'pending'
            elif p.statut and 'cancelled' in p.statut.lower():
                status = 'cancelled'
            
            payments_list.append({
                'id': p.id,
                'reference': p.reference or f'PAY{p.id:06d}',
                'amount': float(p.montant),
                'date_paiement': p.date_paiement.isoformat() if p.date_paiement else None,
                'paid_date': p.date_paiement.isoformat() if p.date_paiement else None,
                'due_date': p.date_paiement.isoformat() if p.date_paiement else None,  # Simplified
                'mode_paiement': p.mode_paiement or 'Espèces',
                'statut': status,
                'formation_nom': formation_name,
                'remarques': p.remarques or '',
                'is_overdue': False,  # Simplified - all paid payments are not overdue
                'total_amount': float(p.montant),  # For compatibility
            })
        
        return JsonResponse({
            'success': True,
            'summary': {
                'total_due': round(total_due, 2),
                'total_paid': round(total_paid, 2),
                'remaining': round(remaining, 2),
                'payment_progress': round(progress, 1),
                'overdue_payments': overdue_count,
            },
            'payments': payments_list,
            'total': len(payments_list)
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': f'Erreur serveur: {str(e)}'
        }, status=500)


@csrf_exempt
@require_http_methods(["POST"])
def api_mobile_student_profile_update(request):
    """
    Update student profile information.
    POST /api/mobile/v2/student/profile/update/
    Body: { "student_id": "123", "field": "value", ... }
    """
    try:
        data = json.loads(request.body)
        student_id = data.get('student_id')
        
        if not student_id:
            return JsonResponse({
                'success': False,
                'error': 'student_id requis'
            }, status=400)
        
        try:
            etudiant = Etudiant.objects.get(pk=int(student_id))
        except (Etudiant.DoesNotExist, ValueError):
            return JsonResponse({
                'success': False,
                'error': 'Étudiant non trouvé'
            }, status=404)
        
        # Update allowed fields
        updatable_fields = [
            'telephone', 'mobile', 'email', 'adresse',
            'niveau_etude', 'situation_professionnelle',
            'nin', 'lieu_naissance', 'nationalite'
        ]
        
        updated_fields = []
        for field in updatable_fields:
            if field in data:
                setattr(etudiant, field, data[field])
                updated_fields.append(field)
        
        if updated_fields:
            etudiant.save()
        
        return JsonResponse({
            'success': True,
            'message': 'Profil mis à jour avec succès',
            'updated_fields': updated_fields
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': f'Erreur serveur: {str(e)}'
        }, status=500)
