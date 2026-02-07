from django.shortcuts import get_object_or_404
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.db.models import Sum, Q
import json
from datetime import datetime
from .models import Etudiant, Inscription, Paiement, Formation

def _format_date(date_obj):
    if date_obj:
        return date_obj.strftime('%Y-%m-%d')
    return None

def _format_datetime(date_obj):
    if date_obj:
        return date_obj.strftime('%Y-%m-%d %H:%M:%S')
    return None

@csrf_exempt
def api_mobile_student_login(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Méthode non autorisée'}, status=405)
    
    try:
        data = json.loads(request.body)
        student_id = data.get('student_id')
        
        if not student_id:
            return JsonResponse({'success': False, 'message': 'Identifiant requis'}, status=400)
            
        # Try by ID first, then email/phone if needed
        student = None
        if str(student_id).isdigit():
            student = Etudiant.objects.filter(id=student_id).first()
        
        if not student:
            student = Etudiant.objects.filter(Q(email=student_id) | Q(telephone=student_id)).first()
            
        if not student:
            return JsonResponse({'success': False, 'message': 'Étudiant non trouvé'}, status=404)
            
        # Get inscription info
        inscriptions = Inscription.objects.filter(etudiant=student)
        inscriptions_count = inscriptions.count()
        
        # Calculate balance
        total_due = 0
        total_paid = 0
        
        for inscription in inscriptions:
            if hasattr(inscription, 'formation') and inscription.formation:
                price = getattr(inscription, 'prix_formation', None) or getattr(inscription.formation, 'prix', 0) or 0
                total_due += price
                
        payments = Paiement.objects.filter(etudiant=student)
        total_paid = payments.aggregate(Sum('montant'))['montant__sum'] or 0
        
        balance = total_due - total_paid
        
        # Get photo URL safely
        photo_url = None
        if hasattr(student, 'image') and student.image:
            try:
                photo_url = student.image.url
            except:
                pass
        
        student_data = {
            'id': str(student.id),
            'nom': getattr(student, 'nom', ''),
            'prenom': getattr(student, 'prenom', ''),
            'email': getattr(student, 'email', ''),
            'telephone': getattr(student, 'telephone', ''),
            'mobile': getattr(student, 'mobile', ''),
            'photo': photo_url,
            'statut': 'Actif' if inscriptions_count > 0 else 'Inactif',
            'date_naissance': _format_date(getattr(student, 'date_naissance', None)),
            'lieu_naissance': getattr(student, 'lieu_naissance', ''),
            'nationalite': getattr(student, 'nationalite', ''),
            'adresse': getattr(student, 'adresse', ''),
            'niveau_etude': getattr(student, 'niveau_etude', ''),
            'situation_professionnelle': getattr(student, 'situation_professionnelle', ''),
            'nin': getattr(student, 'nin', ''),
            'date_inscription': _format_datetime(getattr(student, 'date_created', None)),
            'formation_nom': inscriptions.last().formation.nom if inscriptions.exists() and inscriptions.last().formation else None,
            'groupe_nom': None,
            'balance': float(balance),
            'inscriptions_count': inscriptions_count,
        }
        
        return JsonResponse({
            'success': True,
            'student': student_data
        })
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({'success': False, 'message': str(e)}, status=500)

@csrf_exempt
def api_mobile_student_dashboard(request):
    try:
        student_id = request.GET.get('student_id')
        if not student_id:
            return JsonResponse({'success': False, 'message': 'ID requis'}, status=400)
            
        student = get_object_or_404(Etudiant, id=student_id)
        
        inscriptions = Inscription.objects.filter(etudiant=student)
        payments = Paiement.objects.filter(etudiant=student)
        
        total_due = 0
        for ins in inscriptions:
            price = getattr(ins, 'prix_formation', None) or (getattr(ins.formation, 'prix', 0) if ins.formation else 0) or 0
            total_due += price
            
        total_paid = payments.aggregate(Sum('montant'))['montant__sum'] or 0
        remaining = total_due - total_paid
        progress = (total_paid / total_due * 100) if total_due > 0 else 0
        
        recent_activity = []
        for p in payments.order_by('-date_paiement')[:3]:
            recent_activity.append({
                'title': 'Paiement effectué',
                'description': f"{p.montant} FCFA - {getattr(p, 'motif', 'Frais')}",
                'date': _format_date(p.date_paiement),
                'type': 'payment'
            })
            
        return JsonResponse({
            'success': True,
            'financial_summary': {
                'total_due': float(total_due),
                'total_paid': float(total_paid),
                'remaining': float(remaining),
                'payment_progress': float(progress)
            },
            'upcoming_events': [],
            'recent_activity': recent_activity,
            'news': []
        })
    except Exception as e:
        return JsonResponse({'success': False, 'message': str(e)}, status=500)

@csrf_exempt
def api_mobile_student_formations(request):
    try:
        student_id = request.GET.get('student_id')
        if not student_id:
            return JsonResponse({'success': False, 'message': 'ID requis'}, status=400)
            
        student = get_object_or_404(Etudiant, id=student_id)
        inscriptions = Inscription.objects.filter(etudiant=student).select_related('formation')
        
        formations_list = []
        for ins in inscriptions:
            formation = ins.formation
            if not formation:
                continue
                
            formation_price = getattr(ins, 'prix_formation', None) or getattr(formation, 'prix', 0) or 0
            
            photo_url = None
            if hasattr(formation, 'image') and formation.image:
                try:
                    photo_url = formation.image.url
                except:
                    pass
            
            formations_list.append({
                'id': formation.id,
                'nom': formation.nom,
                'description': getattr(formation, 'description', ''),
                'date_debut': _format_date(getattr(formation, 'date_debut', None)),
                'date_fin': _format_date(getattr(formation, 'date_fin', None)),
                'prix': float(formation_price),
                'photo': photo_url,
                'categorie': formation.categorie.nom if hasattr(formation, 'categorie') and formation.categorie else None,
                'statut': 'En cours',
                'progress_percent': 0,
                'modules_count': 0,
                'paid': 0,
                'remaining': float(formation_price),
                'payment_progress': 0,
                'instructor': None,
                'duree': f"{formation.duree_mois} mois" if hasattr(formation, 'duree_mois') and formation.duree_mois else None
            })
            
        return JsonResponse({
            'success': True,
            'formations': formations_list
        })
    except Exception as e:
        return JsonResponse({'success': False, 'message': str(e)}, status=500)

@csrf_exempt
def api_mobile_student_payments(request):
    try:
        student_id = request.GET.get('student_id')
        if not student_id:
            return JsonResponse({'success': False, 'message': 'ID requis'}, status=400)
            
        student = get_object_or_404(Etudiant, id=student_id)
        payments = Paiement.objects.filter(etudiant=student).order_by('-date_paiement')
        
        payments_list = []
        for p in payments:
            formation_nom = None
            if hasattr(p, 'inscription') and p.inscription and p.inscription.formation:
                formation_nom = p.inscription.formation.nom
                
            payments_list.append({
                'id': p.id,
                'montant': float(p.montant),
                'date': _format_date(p.date_paiement),
                'motif': getattr(p, 'motif', ''),
                'type': getattr(p, 'mode_paiement', ''),
                'statut': 'Validé',
                'reference': getattr(p, 'code_transaction', ''),
                'formation_nom': formation_nom
            })
            
        return JsonResponse({
            'success': True,
            'payments': payments_list,
            'summary': {
                'total_paid': float(payments.aggregate(Sum('montant'))['montant__sum'] or 0),
                'next_payment_date': None,
                'next_payment_amount': 0
            }
        })
    except Exception as e:
        return JsonResponse({'success': False, 'message': str(e)}, status=500)

@csrf_exempt
def api_mobile_student_profile_update(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'POST requis'}, status=405)
        
    try:
        data = json.loads(request.body)
        student_id = data.get('student_id')
        
        if not student_id:
            return JsonResponse({'success': False, 'message': 'ID requis'}, status=400)
            
        student = get_object_or_404(Etudiant, id=student_id)
        
        if 'email' in data: student.email = data['email']
        if 'telephone' in data: student.telephone = data['telephone']
        if 'adresse' in data: student.adresse = data['adresse']
        if 'nin' in data: student.nin = data['nin']
        if 'lieu_naissance' in data: student.lieu_naissance = data['lieu_naissance']
        if 'nationalite' in data: student.nationalite = data['nationalite']
        if 'date_naissance' in data:
            try:
                student.date_naissance = datetime.strptime(data['date_naissance'], '%Y-%m-%d').date()
            except:
                pass
                
        student.save()
        
        return JsonResponse({
            'success': True,
            'message': 'Profil mis à jour'
        })
    except Exception as e:
        return JsonResponse({'success': False, 'message': str(e)}, status=500)
