from django.shortcuts import get_object_or_404
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.db.models import Sum, Q
import json
from datetime import datetime
from Schoolapp.models import Etudiant, Inscription, Paiement, Formation, Module, Parametre

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
            
        # Try by ID first, then email/phone if needed (but currently simplified to ID)
        student = None
        if str(student_id).isdigit():
            student = Etudiant.objects.filter(id=student_id).first()
        
        if not student:
            # Fallback for email/phone login if student_id is string
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
                # Logic for pricing (simplified)
                price = inscription.prix_formation or inscription.formation.prix or 0
                total_due += price
                
        payments = Paiement.objects.filter(etudiant=student)
        total_paid = payments.aggregate(Sum('montant'))['montant__sum'] or 0
        
        balance = total_due - total_paid
        
        student_data = {
            'id': str(student.id),
            'nom': student.nom,
            'prenom': student.prenom,
            'email': student.email,
            'telephone': student.telephone,
            'mobile': student.mobile,
            'photo': student.image.url if student.image else None,
            'statut': 'Actif' if inscriptions_count > 0 else 'Inactif',
            'date_naissance': _format_date(student.date_naissance),
            'lieu_naissance': student.lieu_naissance,
            'nationalite': student.nationalite,
            'adresse': student.adresse,
            'niveau_etude': student.niveau_etude,
            'situation_professionnelle': student.situation_professionnelle,
            'nin': student.nin,
            'date_inscription': _format_datetime(student.date_created) if hasattr(student, 'date_created') else None,
            'formation_nom': inscriptions.last().formation.nom if inscriptions.exists() else None,
            'groupe_nom': None, # TODO
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
        
        # Financial Summary
        inscriptions = Inscription.objects.filter(etudiant=student)
        payments = Paiement.objects.filter(etudiant=student)
        
        total_due = 0
        for ins in inscriptions:
            total_due += (ins.prix_formation or (ins.formation.prix if ins.formation else 0) or 0)
            
        total_paid = payments.aggregate(Sum('montant'))['montant__sum'] or 0
        remaining = total_due - total_paid
        progress = (total_paid / total_due * 100) if total_due > 0 else 0
        
        # Recent Activity (mock for now or real if available)
        recent_activity = []
        for p in payments.order_by('-date_paiement')[:3]:
            recent_activity.append({
                'title': 'Paiement effectué',
                'description': f"{p.montant} FCFA - {p.motif or 'Frais de scolarité'}",
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
                
            # Calculate progress (mock or real modules completion)
            progress = 0 # TODO: Calculate from module completion
            
            # Calculate payment status for this formation
            formation_price = ins.prix_formation or formation.prix or 0
            # Simplify: assume global payment coverage or specific attribution if available
            # Here we just show global status or simplistic logic
            paid = 0 # complex to track per formation without specific link in Paiement
            
            formations_list.append({
                'id': formation.id,
                'nom': formation.nom,
                'description': formation.description,
                'date_debut': _format_date(formation.date_debut),
                'date_fin': _format_date(formation.date_fin),
                'prix': float(formation_price),
                'photo': formation.image.url if formation.image else None,
                'categorie': formation.categorie.nom if formation.categorie else None,
                'statut': 'En cours', # active/completed based on date
                'progress_percent': progress,
                'modules_count': Module.objects.filter(formation=formation).count(),
                'paid': float(paid),
                'remaining': float(formation_price - paid),
                'payment_progress': 0, # TODO
                'instructor': None, # TODO
                'duree': f"{formation.duree_mois} mois" if formation.duree_mois else None
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
            payments_list.append({
                'id': p.id,
                'montant': float(p.montant),
                'date': _format_date(p.date_paiement),
                'motif': p.motif,
                'type': p.mode_paiement,
                'statut': 'Validé', # Default
                'reference': p.code_transaction,
                'formation_nom': p.inscription.formation.nom if p.inscription and p.inscription.formation else None
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
        
        # Update fields
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
                pass # Ignore bad date format
                
        student.save()
        
        return JsonResponse({
            'success': True,
            'message': 'Profil mis à jour'
        })
    except Exception as e:
        return JsonResponse({'success': False, 'message': str(e)}, status=500)
