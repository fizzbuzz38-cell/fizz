from django.shortcuts import get_object_or_404
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.db.models import Sum, Q
import json
from datetime import datetime
from decimal import Decimal
from .models import Etudiant, Inscription, Paiement, Formation, Module

def _format_date(date_obj):
    if date_obj:
        return date_obj.strftime('%Y-%m-%d')
    return None

def _format_datetime(date_obj):
    if date_obj:
        return date_obj.strftime('%Y-%m-%d %H:%M:%S')
    return None

def _to_float(value):
    """Safely convert Decimal or any numeric to float"""
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    return float(value)

def _get_inscription_price(inscription):
    """Get the price for an inscription - use prix_total or formation's prix_etudiant"""
    if inscription.prix_total:
        return _to_float(inscription.prix_total)
    if inscription.formation:
        return _to_float(inscription.formation.prix_etudiant or 0)
    return 0.0

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
        inscriptions = Inscription.objects.filter(etudiant=student).select_related('formation')
        inscriptions_count = inscriptions.count()
        
        # Calculate balance using correct field names
        total_due = sum(_get_inscription_price(ins) for ins in inscriptions)
                
        payments = Paiement.objects.filter(etudiant=student)
        total_paid = _to_float(payments.aggregate(Sum('montant'))['montant__sum'] or 0)
        
        balance = total_due - total_paid
        
        # Get photo URL safely
        photo_url = None
        if student.photo:
            photo_url = student.photo
        
        student_data = {
            'id': str(student.id),
            'nom': student.nom or '',
            'prenom': student.prenom or '',
            'email': student.email or '',
            'telephone': student.telephone or '',
            'mobile': student.mobile or '',
            'photo': photo_url,
            'statut': 'Actif' if inscriptions_count > 0 else 'Inactif',
            'date_naissance': _format_date(student.date_naissance),
            'lieu_naissance': student.lieu_naissance or '',
            'nationalite': student.nationalite or '',
            'adresse': student.adresse or '',
            'niveau_etude': student.niveau_etude or '',
            'situation_professionnelle': student.situation_professionnelle or '',
            'nin': student.nin or '',
            'date_inscription': _format_datetime(student.date_created) if hasattr(student, 'date_created') and student.date_created else None,
            'formation_nom': inscriptions.last().formation.nom if inscriptions.exists() and inscriptions.last().formation else None,
            'groupe_nom': inscriptions.last().groupe.nom if inscriptions.exists() and inscriptions.last().groupe else None,
            'balance': balance,
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
        
        inscriptions = Inscription.objects.filter(etudiant=student).select_related('formation')
        payments = Paiement.objects.filter(etudiant=student)
        
        # Calculate totals using correct field names
        total_due = sum(_get_inscription_price(ins) for ins in inscriptions)
            
        total_paid = _to_float(payments.aggregate(Sum('montant'))['montant__sum'] or 0)
        remaining = total_due - total_paid
        progress = (total_paid / total_due * 100) if total_due > 0 else 0
        
        recent_activity = []
        for p in payments.order_by('-date_paiement')[:3]:
            formation_nom = p.formation.nom if p.formation else 'Formation'
            recent_activity.append({
                'title': 'Paiement effectué',
                'description': f"{_to_float(p.montant)} DZD - {formation_nom}",
                'date': _format_date(p.date_paiement),
                'type': 'payment'
            })
            
        return JsonResponse({
            'success': True,
            'financial_summary': {
                'total_due': total_due,
                'total_paid': total_paid,
                'remaining': remaining,
                'payment_progress': progress
            },
            'upcoming_events': [],
            'recent_activity': recent_activity,
            'news': []
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({'success': False, 'message': str(e)}, status=500)

@csrf_exempt
def api_mobile_student_formations(request):
    try:
        student_id = request.GET.get('student_id')
        if not student_id:
            # If no student ID provided, return public formations (or error depending on requirements)
            # For now, let's require student ID to check enrollment status
            return JsonResponse({'success': False, 'message': 'ID requis'}, status=400)
            
        student = get_object_or_404(Etudiant, id=student_id)
        
        # Get all inscriptions for this student to check enrollment status
        inscriptions = Inscription.objects.filter(etudiant=student).select_related('formation')
        inscription_map = {ins.formation.id: ins for ins in inscriptions if ins.formation}
        
        # Get all payments for this student
        payments = Paiement.objects.filter(etudiant=student)
        
        # Calculate payments per formation
        payments_by_formation = {}
        for p in payments:
            if p.formation:
                fid = p.formation.id
                if fid not in payments_by_formation:
                    payments_by_formation[fid] = 0
                payments_by_formation[fid] += _to_float(p.montant)
        
        # Get ALL formations
        all_formations = Formation.objects.all()
        
        formations_list = []
        for formation in all_formations:
            # Check if enrolled
            is_enrolled = formation.id in inscription_map
            inscription = inscription_map.get(formation.id)
            
            # Get price
            if is_enrolled and inscription:
                formation_price = _get_inscription_price(inscription)
                statut = inscription.statut or 'inscrit'
                progress = _to_float(inscription.progress_percent or 0)
            else:
                # Default public price
                formation_price = _to_float(formation.prix_etudiant or 0)
                statut = 'non_inscrit'
                progress = 0
            
            # Get photo URL
            photo_url = formation.photo if formation.photo else None
            
            # Get module count
            modules_count = Module.objects.filter(formation=formation).count()
            
            # Calculate payment status
            paid_amount = payments_by_formation.get(formation.id, 0)
            remaining = max(0, formation_price - paid_amount)
            payment_progress = (paid_amount / formation_price * 100) if formation_price > 0 else 0
            
            formations_list.append({
                'id': formation.id,
                'nom': formation.nom,
                'description': formation.contenu or '',
                'date_debut': None,
                'date_fin': None,
                'prix': formation_price,
                'photo': photo_url,
                'categorie': formation.categorie or formation.branche or '',
                'statut': statut,
                'progress_percent': progress,
                'modules_count': modules_count,
                'paid': paid_amount,
                'remaining': remaining,
                'payment_progress': payment_progress,
                'instructor': None,
                'duree': formation.duree or '',
                'is_enrolled': is_enrolled
            })
            
        return JsonResponse({
            'success': True,
            'formations': formations_list
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({'success': False, 'message': str(e)}, status=500)

@csrf_exempt
def api_mobile_student_payments(request):
    try:
        student_id = request.GET.get('student_id')
        if not student_id:
            return JsonResponse({'success': False, 'message': 'ID requis'}, status=400)
            
        student = get_object_or_404(Etudiant, id=student_id)
        payments = Paiement.objects.filter(etudiant=student).select_related('formation', 'inscription').order_by('-date_paiement')
        
        # Calculate total due from inscriptions using correct field names
        inscriptions = Inscription.objects.filter(etudiant=student).select_related('formation')
        total_due = sum(_get_inscription_price(ins) for ins in inscriptions)
        
        total_paid = _to_float(payments.aggregate(Sum('montant'))['montant__sum'] or 0)
        remaining = total_due - total_paid
        payment_progress = (total_paid / total_due * 100) if total_due > 0 else 0
        
        payments_list = []
        for p in payments:
            formation_nom = None
            if p.formation:
                formation_nom = p.formation.nom
            elif p.inscription and p.inscription.formation:
                formation_nom = p.inscription.formation.nom
                
            payments_list.append({
                'id': p.id,
                'montant': _to_float(p.montant),
                'date': _format_date(p.date_paiement),
                'motif': p.remarques or '',
                'type': p.mode_paiement or '',
                'statut': p.statut or 'Validé',
                'reference': p.reference or '',
                'formation_nom': formation_nom,
                'balance_after': _to_float(p.balance_after) if p.balance_after else None
            })
            
        return JsonResponse({
            'success': True,
            'payments': payments_list,
            'summary': {
                'total_due': total_due,
                'total_paid': total_paid,
                'remaining': remaining,
                'payment_progress': payment_progress,
                'next_payment_date': None,
                'next_payment_amount': 0
            }
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
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
        
        if 'nom' in data: student.nom = data['nom']
        if 'prenom' in data: student.prenom = data['prenom']
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

@csrf_exempt
def api_mobile_scan_id_card(request):
    """
    Scan an ID card image using AI (OpenRouter) and extract information.
    Uses OPENROUTER_API_KEY from Railway environment variables.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'POST requis'}, status=405)
    
    try:
        import base64
        import requests
        from django.conf import settings
        import os
        import re
        
        # Get the image from request
        if request.content_type and 'multipart' in request.content_type:
            # Handle multipart form data
            image_file = request.FILES.get('image')
            if not image_file:
                return JsonResponse({'success': False, 'message': 'Image requise'}, status=400)
            image_data = image_file.read()
        else:
            # Handle JSON with base64 image
            data = json.loads(request.body)
            image_base64 = data.get('image')
            if not image_base64:
                return JsonResponse({'success': False, 'message': 'Image requise'}, status=400)
            # Remove data URL prefix if present
            if ',' in image_base64:
                image_base64 = image_base64.split(',')[1]
            image_data = base64.b64decode(image_base64)
        
        # Encode to base64 for API
        base64_image = base64.b64encode(image_data).decode('utf-8')
        
        # Get API key from environment
        api_key = os.environ.get('OPENROUTER_API_KEY') or getattr(settings, 'OPENROUTER_API_KEY', '')
        
        if not api_key:
            return JsonResponse({
                'success': False, 
                'message': 'OPENROUTER_API_KEY non configurée sur le serveur'
            }, status=500)
        
        # Call OpenRouter API
        # Call OpenRouter API
        # Call OpenRouter API
        api_url = 'https://openrouter.ai/api/v1/chat/completions'
        
        # Models to try in order of preference (using robust free tier models)
        # Note: Must use Multimodal models (support images)
        models_to_try = [
            'google/gemini-2.0-flash-lite-preview-02-05:free', # Reliable Multimodal
            'google/gemini-2.0-flash-exp:free', # Backup Multimodal
        ]
        
        prompt = '''Analyse cette carte d'identité biométrique algérienne.
Extrais UNIQUEMENT les champs en ARABE suivants au format JSON strict.

1. Nom (اللقب) : Situé à droite, sous le numéro d'identité.
2. Prénom (الاسم) : Situé sous le Nom.
3. Lieu de Naissance (مكان الميلاد) : Situé en bas à droite.

JSON attendu (sans markdown, sans texte avant/après) :
{
  "nom": "...", 
  "prenom": "...",
  "lieuNaissance": "..."
}
'''
        
        last_error = None
        current_response = None
        import time
        
        for model in models_to_try:
            try:
                response = requests.post(
                    api_url,
                    headers={
                        'Authorization': f'Bearer {api_key}',
                        'Content-Type': 'application/json',
                        'HTTP-Referer': 'https://github.com/fizzbuzz38-cell/fizz',
                        'X-Title': 'StudentApp'
                    },
                    json={
                        'model': model,
                        'messages': [
                            {
                                'role': 'user',
                                'content': [
                                    {'type': 'text', 'text': prompt},
                                    {
                                        'type': 'image_url',
                                        'image_url': {
                                            'url': f'data:image/jpeg;base64,{base64_image}'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    timeout=60
                )
                
                if response.status_code == 200:
                    current_response = response
                    break # Success!
                elif response.status_code == 429:
                    last_error = f"Rate limit (429) on {model}"
                    time.sleep(2) # Wait a bit before trying next model
                    continue # Try next model
                else:
                    last_error = f"Error {response.status_code} on {model}: {response.text}"
                    continue
                    
            except Exception as e:
                last_error = str(e)
                continue
        
        # If no successful response after trying all models
        if not current_response:
             # Fallback mock data if everything fails (for demo purposes)
             # return JsonResponse({'success': False, 'message': f'IA indisponible: {last_error}'}, status=500)
             # Use the last error code or 500
             return JsonResponse({'success': False, 'message': f'Toutes les tentatives ont échoué. Derniere erreur: {last_error}'}, status=500)
             
        response = current_response
        
        if response.status_code != 200:
            return JsonResponse({
                'success': False,
                'message': f'Erreur API: {response.status_code}'
            }, status=500)
        
        # Parse response
        result = response.json()
        content = result['choices'][0]['message']['content']
        
        # Extract JSON from response (might be wrapped in markdown)
        json_content = content.strip()
        if json_content.startswith('```json'):
            json_content = json_content[7:]
        elif json_content.startswith('```'):
            json_content = json_content[3:]
        if json_content.endswith('```'):
            json_content = json_content[:-3]
        json_content = json_content.strip()
        
        # Parse extracted data
        extracted = json.loads(json_content)
        
        return JsonResponse({
            'success': True,
            'data': {
                'nin': extracted.get('nin', ''),
                'nom': extracted.get('nom', ''),
                'prenom': extracted.get('prenom', ''),
                'dateNaissance': extracted.get('dateNaissance', ''),
                'lieuNaissance': extracted.get('lieuNaissance', ''),
            }
        })
        
    except json.JSONDecodeError as e:
        return JsonResponse({
            'success': False,
            'message': f'Erreur parsing JSON: {str(e)}'
        }, status=500)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({
            'success': False,
            'message': f'Erreur: {str(e)}'
        }, status=500)

# Aliases for urls.py compatibility
api_mobile_student_formations_list = api_mobile_student_formations
api_mobile_student_payments_list = api_mobile_student_payments
