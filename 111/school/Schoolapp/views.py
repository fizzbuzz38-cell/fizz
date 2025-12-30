from django.shortcuts import render
from django.http import JsonResponse
from django.db.models import Sum, Avg, Count, Q
from .utils import calculate_balances
from django.core.paginator import Paginator
from .models import (
    Etudiant,
    Formation,
    Enseignant,
    Utilisateur,
    Inscription,
    Paiement,
    SchoolVersement,
    Salle,
    Invoice,
    Module,
    ProgressionModule,
    AvailabilityFormateur,
    Session,
    Certification,
    CalendarEvent,
    Groupe,
    Charge,
)



from .models import Presence
from .forms import PresenceForm
from .models import Fournisseur, Achat, AchatItem, ReglementFournisseur

from django.core.files.storage import default_storage
from django.core.files.base import ContentFile
from django.conf import settings
from decimal import Decimal
from datetime import datetime, date
import json
from django.utils import timezone
import re
from django.views.decorators.csrf import csrf_exempt
from django.shortcuts import redirect
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.http import require_POST

from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_http_methods
from django.http import JsonResponse
from django.urls import reverse
from io import BytesIO
from django.core.mail import send_mail, get_connection
import threading
from django.utils.html import strip_tags
import smtplib
import socket
import os
from datetime import datetime as _dt
import uuid
import traceback
import base64


def paiement_receipt_pdf(request, pk):
    """Generate a PDF (A5 landscape) containing two receipt coupons for a Paiement.

    This view attempts to import WeasyPrint on demand. If WeasyPrint is not
    installed, it returns a helpful HTTP 500 with instructions.
    """
    paiement = get_object_or_404(Paiement, pk=pk)
    # Build context similarly to paiement_receipt view but limit formations to those
    # actually associated with this Paiement (inscription -> formation, else paiement.formation).
    etudiant = getattr(paiement, 'etudiant', None)
    formation = getattr(paiement, 'formation', None)

    # montant safe conversion
    try:
        montant = float(paiement.montant)
    except Exception:
        montant = paiement.montant

    # compute balances for this payment (ancien/apres)
    ancien_solde = None
    apres_solde = None
    try:
        balances = calculate_balances(paiement.etudiant.id if paiement.etudiant else None, payment_date=paiement.date_paiement, montant=paiement.montant, exclude_payment_pk=paiement.pk)
        ancien_solde = balances.get('ancien_solde')
        apres_solde = balances.get('apres_solde')
    except Exception:
        ancien_solde = getattr(paiement, 'ancien_solde', None)
        apres_solde = getattr(paiement, 'apres_solde', None)

    # determine formations_list from the payment's links only
    formations_list = []
    try:
        if getattr(paiement, 'inscription', None) and getattr(paiement.inscription, 'formation', None):
            fname = getattr(paiement.inscription.formation, 'nom', None)
            if fname:
                formations_list = [fname]
        elif formation:
            fname = getattr(formation, 'nom', None)
            if fname:
                formations_list = [fname]
    except Exception:
        formations_list = []

    context = {
        'paiement': paiement,
        'etudiant': etudiant,
        'formation': formation,
        'formations_list': formations_list,
        'montant': montant,
        'total_frais': getattr(paiement, 'prix_total', None),
        'ancien_solde': ancien_solde,
        'apres_solde': apres_solde,
        'statut_label': paiement.statut,
        'frais_inscription_included': True if (getattr(paiement, 'remarques', '') and 'Frais inscription' in str(getattr(paiement, 'remarques', ''))) else False,
        'frais_inscription_amount': 500.00 if (getattr(paiement, 'remarques', '') and 'Frais inscription' in str(getattr(paiement, 'remarques', ''))) else None,
    }

    html = render_to_string('paiement_receipt.html', context)

    try:
        # import lazily so app doesn't require weasyprint for unrelated operations
        from weasyprint import HTML, CSS
    except Exception as e:
        msg = ("WeasyPrint is not installed or failed to import. To enable PDF generation, "
               "install it with: pip install weasyprint\n" + str(e))
        return HttpResponse(msg, status=500, content_type='text/plain')

    # Create PDF with A5 landscape page size (210mm x 148mm)
    pdf_io = BytesIO()
    css = CSS(string='@page { size: 210mm 148mm; margin: 10mm; }')
    HTML(string=html, base_url=request.build_absolute_uri('/')).write_pdf(pdf_io, stylesheets=[css])
    pdf_io.seek(0)
    resp = HttpResponse(pdf_io.read(), content_type='application/pdf')
    resp['Content-Disposition'] = f'inline; filename="paiement_{paiement.pk}_receipt.pdf"'
    return resp


@require_POST
def toggle_active_db(request):
    """AJAX endpoint: set or toggle the per-session active DB alias.

    POST params:
      - db: optional, the desired DB alias (e.g. 'schooloctobre' or 'default').
    If absent, this view toggles between 'default' and 'schooloctobre' when
    both are available in settings.DATABASES.
    The selected alias is stored in `request.session['active_db']` and a cookie
    is set for convenience. The middleware/router will read this value on
    subsequent requests and route DB operations accordingly.
    """
    try:
        from django.conf import settings
        alias = request.POST.get('db')
        # validate provided alias
        if alias and alias not in settings.DATABASES:
            return JsonResponse({'ok': False, 'error': 'Unknown database alias'}, status=400)
        # toggle if no alias provided
        if not alias:
            current = None
            try:
                current = request.session.get('active_db')
            except Exception:
                current = None
            # pick alternate if present
            if 'schooloctobre' in settings.DATABASES:
                alias = 'schooloctobre' if current != 'schooloctobre' else 'default'
            else:
                alias = 'default'
        # persist in session
        try:
            request.session['active_db'] = alias
        except Exception:
            pass
        resp = JsonResponse({'ok': True, 'active_db': alias})
        # also set cookie for middleware fallback
        resp.set_cookie('active_db', alias, max_age=60*60*24*30)
        return resp
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=500)








def _write_email_log(msg: str):
    """Append a timestamped message to BASE_DIR/email_sends.log for debugging.

    This helper is intentionally small and robust: failures to write the log
    are ignored so email sending doesn't crash the app.
    """
    try:
        base = getattr(settings, 'BASE_DIR', None)
        if not base:
            return
        path = os.path.join(str(base), 'email_sends.log')
        ts = _dt.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        with open(path, 'a', encoding='utf-8') as f:
            f.write(f"[{ts} UTC] {msg}\n")
    except Exception:
        # don't raise from logging
        pass
def _safe_send(subject, to_email, template_name, context, force_smtp=False):
    """Render template and send email; exceptions are caught and printed."""

    try:
        html_message = render_to_string(template_name, context)
        plain_message = strip_tags(html_message)
        print('[email] _safe_send: rendered template, length:', len(html_message))

        from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', None) or getattr(settings, 'EMAIL_HOST_USER', None) or 'noreply@example.com'

        # Use an explicit connection so we can enable smtplib debug output when
        # the SMTP backend is configured. This will print the SMTP protocol
        # exchange to stdout which helps diagnose auth/connection issues.
        conn = None

        # ensure network calls don't hang indefinitely; use a reasonable default timeout
        old_timeout = None
        try:
            old_timeout = socket.getdefaulttimeout()
            socket.setdefaulttimeout(10)
        except Exception:
            old_timeout = None

        try:
            # By default use the project's configured email backend. When
            # force_smtp=True we explicitly request the SMTP backend so that
            # emails are sent over the network even when DEBUG activates the
            # console backend for local development.
            if force_smtp:
                # Build explicit SMTP connection parameters using environment
                # variables when available. Django's settings object exposes
                # defaults (e.g. EMAIL_HOST='localhost', EMAIL_PORT=25) which
                # we must avoid when forcing SMTP for real network sends in
                # development. Prefer explicit env vars, then fall back to
                # the conventional Gmail defaults.
                host = os.environ.get('EMAIL_HOST', 'smtp.gmail.com')
                port = int(os.environ.get('EMAIL_PORT', '587'))
                username = os.environ.get('EMAIL_HOST_USER', getattr(settings, 'EMAIL_HOST_USER', None))
                password = os.environ.get('EMAIL_HOST_PASSWORD', getattr(settings, 'EMAIL_HOST_PASSWORD', None))
                use_tls = os.environ.get('EMAIL_USE_TLS', None)
                if use_tls is None:
                    use_tls = bool(getattr(settings, 'EMAIL_USE_TLS', True))
                else:
                    use_tls = str(use_tls).lower() in ('1', 'true', 'yes')
                conn = get_connection(
                    backend='django.core.mail.backends.smtp.EmailBackend',
                    host=host,
                    port=port,
                    username=username,
                    password=password,
                    use_tls=use_tls,
                )
            else:
                conn = get_connection()
            # ensure the underlying connection object is created/opened; some backends lazily open
            try:
                conn.open()
            except Exception as e_open:
                print('[email] _safe_send: connection.open() failed:', e_open)

            # if the underlying object exposes set_debuglevel (smtplib.SMTP), enable it
            try:
                if hasattr(conn, 'connection') and hasattr(conn.connection, 'set_debuglevel'):
                    conn.connection.set_debuglevel(1)
                    print('[email] _safe_send: enabled smtplib debuglevel=1')
            except Exception:
                pass

        except Exception as e_conn:
            print('[email] _safe_send: get_connection() failed:', e_conn)
            conn = None

        # If we have a connection object, pass it to send_mail so it reuses it.
        sent_ok = False
        send_error = None
        try:
            if conn:
                send_mail(subject, plain_message, from_email, [to_email], fail_silently=False, html_message=html_message, connection=conn)
            else:
                send_mail(subject, plain_message, from_email, [to_email], fail_silently=False, html_message=html_message)
            sent_ok = True
            print(f"[email] _safe_send: send_mail returned for {to_email}")
        except Exception as send_err:
            print('[email] _safe_send: send_mail raised:', send_err)
            send_error = str(send_err)
            # Fallback: try a direct smtplib connection with a short timeout to get clear error
            try:
                # Prefer env vars to avoid accidentally connecting to the
                # local MTA (localhost:25) which is the Django default.
                host = os.environ.get('EMAIL_HOST', getattr(settings, 'EMAIL_HOST', 'smtp.gmail.com'))
                port = int(os.environ.get('EMAIL_PORT', getattr(settings, 'EMAIL_PORT', 587)))
                use_tls_env = os.environ.get('EMAIL_USE_TLS', None)
                if use_tls_env is None:
                    use_tls = bool(getattr(settings, 'EMAIL_USE_TLS', True))
                else:
                    use_tls = str(use_tls_env).lower() in ('1', 'true', 'yes')
                user = os.environ.get('EMAIL_HOST_USER', getattr(settings, 'EMAIL_HOST_USER', None))
                pwd = os.environ.get('EMAIL_HOST_PASSWORD', getattr(settings, 'EMAIL_HOST_PASSWORD', None))
                print(f'[email] _safe_send: attempting direct smtplib connect to {host}:{port} (tls={use_tls}) with timeout=10s')
                s = smtplib.SMTP(host, port, timeout=10)
                s.set_debuglevel(1)
                if use_tls:
                    s.starttls()
                if user and pwd:
                    s.login(user, pwd)
                from email.message import EmailMessage
                msg = EmailMessage()
                msg['Subject'] = subject
                msg['From'] = from_email
                msg['To'] = to_email
                msg.set_content(plain_message)
                msg.add_alternative(html_message, subtype='html')
                s.send_message(msg)
                s.quit()
                sent_ok = True
                send_error = None
                print(f'[email] _safe_send: smtplib fallback send_message succeeded for {to_email}')
            except (smtplib.SMTPException, socket.timeout, OSError) as fallback_err:
                print('[email] _safe_send: smtplib fallback failed:', fallback_err)
                send_error = send_error or str(fallback_err)

        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass
            try:
                if old_timeout is not None:
                    socket.setdefaulttimeout(old_timeout)
            except Exception:
                pass

        # Return tuple (ok, error_message_or_None)
        _write_email_log(f"SEND_RESULT to={to_email} ok={bool(sent_ok)} error={send_error}")
        return (bool(sent_ok), send_error)
    except Exception as e:
        try:
            print('Error sending email to', to_email, e)
        except Exception:
            pass
        # Compute student's current balance using shared util so behaviour matches students list
        try:
            from .utils import calculate_balances
            balances = calculate_balances(ins.etudiant.id if getattr(ins, 'etudiant', None) else None)
            val = balances.get('ancien_solde', None)
            # store as string for template rendering; keep a regle boolean for zero-checks
            try:
                ins.remaining_fees = str(val if val is not None else 0)
            except Exception:
                ins.remaining_fees = '0'
            try:
                ins.regle = abs(float(val or 0.0)) < 1e-6
            except Exception:
                ins.regle = False
        except Exception:
            ins.remaining_fees = '0'
            ins.regle = False
        _write_email_log(f"EXC to={to_email} err={e}")
        return (False, str(e))







def send_welcome_email_async(to_email, context, force_smtp=False):
    """Start a background thread to send the welcome email using a template.

    If force_smtp is True the background thread will explicitly use the SMTP
    backend so emails are sent over the network even when DEBUG activates the
    console backend.
    """
    subject = "Bienvenue à GénieSchool"
    template_name = 'email/welcome_etudiant.html'
    print(f"[email] scheduling welcome email to {to_email} (force_smtp={force_smtp})")

    def _worker(force_smtp_local=False):
        # perform a small retry loop to improve chance of delivery in flaky networks
        attempts = 0
        max_attempts = 3
        wait_seconds = 3
        last_err = None
        while attempts < max_attempts:
            attempts += 1
            try:
                print(f"[email-thread] attempt {attempts}/{max_attempts} sending to {to_email} (force_smtp={force_smtp_local})")
                ok, err = _safe_send(subject, to_email, template_name, context, force_smtp=force_smtp_local)
                if ok:
                    print(f"[email-thread] send succeeded to {to_email} on attempt {attempts}")
                    _write_email_log(f"ATTEMPT_OK to={to_email} attempt={attempts}")
                    return True
                else:
                    last_err = err
                    print(f"[email-thread] send failed to {to_email} on attempt {attempts}: {err}")
                    _write_email_log(f"ATTEMPT_FAIL to={to_email} attempt={attempts} err={err}")
            except Exception as e:
                last_err = str(e)
                print(f"[email-thread] unexpected exception when sending to {to_email} on attempt {attempts}: {e}")
            # backoff before retrying
            try:
                if attempts < max_attempts:
                    print(f"[email-thread] will retry in {wait_seconds} seconds...")
                    import time
                    time.sleep(wait_seconds)
                    wait_seconds *= 2
            except Exception:
                pass
        print(f"[email-thread] all attempts failed to send to {to_email}; last error: {last_err}")
        _write_email_log(f"ALL_FAILED to={to_email} last_err={last_err}")
        return False

    # start background thread and pass the requested force_smtp flag
    t = threading.Thread(target=_worker, args=(force_smtp,))
    t.daemon = True
    t.start()


def send_paiement_receipt_async(to_email, context, force_smtp=False):
    """Send the paiement receipt email in a background thread using the
    `paiement_receipt.html` template. Mirrors the retry behaviour of the
    welcome email helper.
    """
    subject = "Votre reçu de paiement - GénieSchool"
    template_name = 'paiement_receipt.html'
    print(f"[email] scheduling paiement receipt to {to_email} (force_smtp={force_smtp})")

    def _worker(force_smtp_local=False):
        attempts = 0
        max_attempts = 3
        wait_seconds = 3
        last_err = None
        while attempts < max_attempts:
            attempts += 1
            try:
                print(f"[email-thread][receipt] attempt {attempts}/{max_attempts} sending to {to_email} (force_smtp={force_smtp_local})")
                ok, err = _safe_send(subject, to_email, template_name, context, force_smtp=force_smtp_local)
                if ok:
                    print(f"[email-thread][receipt] send succeeded to {to_email} on attempt {attempts}")
                    _write_email_log(f"RECEIPT_ATTEMPT_OK to={to_email} attempt={attempts}")
                    return True
                else:
                    last_err = err
                    print(f"[email-thread][receipt] send failed to {to_email} on attempt {attempts}: {err}")
                    _write_email_log(f"RECEIPT_ATTEMPT_FAIL to={to_email} attempt={attempts} err={err}")
            except Exception as e:
                last_err = str(e)
                print(f"[email-thread][receipt] unexpected exception when sending to {to_email} on attempt {attempts}: {e}")
            # backoff
            try:
                if attempts < max_attempts:
                    import time
                    time.sleep(wait_seconds)
                    wait_seconds *= 2
            except Exception:
                pass
        print(f"[email-thread][receipt] all attempts failed to send to {to_email}; last error: {last_err}")
        _write_email_log(f"RECEIPT_ALL_FAILED to={to_email} last_err={last_err}")
        return False

    t = threading.Thread(target=_worker, args=(force_smtp,))
    t.daemon = True
    t.start()
    return t


def _smtplib_send_thread(to_email, subject, html_message, plain_message=None):
    """Start a background thread that sends the given message via smtplib

    This uses environment variables (or settings) for SMTP host/port/user/pwd
    and mirrors the manual test you provided. It runs in a daemon thread so
    it doesn't block the request/response cycle.
    """
    def _worker():
        try:
            host = os.environ.get('EMAIL_HOST', getattr(settings, 'EMAIL_HOST', 'smtp.gmail.com'))
            port = int(os.environ.get('EMAIL_PORT', getattr(settings, 'EMAIL_PORT', 587)))
            user = os.environ.get('EMAIL_HOST_USER', getattr(settings, 'EMAIL_HOST_USER', None))
            pwd = os.environ.get('EMAIL_HOST_PASSWORD', getattr(settings, 'EMAIL_HOST_PASSWORD', None))
            use_tls_env = os.environ.get('EMAIL_USE_TLS', None)
            if use_tls_env is None:
                use_tls = bool(getattr(settings, 'EMAIL_USE_TLS', True))
            else:
                use_tls = str(use_tls_env).lower() in ('1', 'true', 'yes')

            _write_email_log(f"SMTPLIB_START to={to_email} host={host} port={port} tls={use_tls}")

            s = smtplib.SMTP(host, port, timeout=15)
            # keep debuglevel off in production; use 1 for troubleshooting
            s.set_debuglevel(0)
            s.ehlo()
            if use_tls:
                s.starttls()
                s.ehlo()
            if user and pwd:
                try:
                    s.login(user, pwd)
                except Exception as e:
                    _write_email_log(f"SMTPLIB_LOGIN_FAIL to={to_email} err={e}")
                    try:
                        s.quit()
                    except Exception:
                        pass
                    return

            from email.message import EmailMessage
            msg = EmailMessage()
            msg['Subject'] = subject
            msg['From'] = user or getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@example.com')
            msg['To'] = to_email
            if plain_message:
                msg.set_content(plain_message)
            else:
                msg.set_content(strip_tags(html_message))
            if html_message:
                msg.add_alternative(html_message, subtype='html')

            try:
                s.send_message(msg)
                _write_email_log(f"SMTPLIB_SENT to={to_email}")
            except Exception as e:
                _write_email_log(f"SMTPLIB_SEND_FAIL to={to_email} err={e}")
            try:
                s.quit()
            except Exception:
                pass
        except Exception as e:
            _write_email_log(f"SMTPLIB_EXC to={to_email} err={e}")

    t = threading.Thread(target=_worker)
    t.daemon = True
    t.start()


def _smtplib_send_direct_thread(to_email, subject=None, html_message=None, plain_message=None):
    """Run the exact smtplib sequence used in the user's test in a background thread.

    This mirrors the one-off script: connect to smtp.gmail.com:587, ehlo, starttls,
    login with credentials, send a simple text message and quit. It writes to the
    persistent email log and prints OK_SENT on success (server console).
    """
    def _worker():
        try:
            host = os.environ.get('EMAIL_HOST', 'smtp.gmail.com')
            port = int(os.environ.get('EMAIL_PORT', '587'))
            user = os.environ.get('EMAIL_HOST_USER', getattr(settings, 'EMAIL_HOST_USER', None))
            pwd = os.environ.get('EMAIL_HOST_PASSWORD', getattr(settings, 'EMAIL_HOST_PASSWORD', None))

            _write_email_log(f"SMTPLIB_DIRECT_START to={to_email} host={host} port={port}")

            s = smtplib.SMTP(host, port, timeout=15)
            s.set_debuglevel(1)
            s.ehlo()
            s.starttls()
            s.ehlo()
            if user and pwd:
                s.login(user, pwd)

            from email.message import EmailMessage
            msg = EmailMessage()
            msg['Subject'] = subject or 'Bienvenue à GénieSchool'
            msg['From'] = user or getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@example.com')
            msg['To'] = to_email

            # If we received an HTML message, include it; otherwise fall back to simple text
            if html_message:
                if plain_message:
                    msg.set_content(plain_message)
                else:
                    msg.set_content(strip_tags(html_message))
                msg.add_alternative(html_message, subtype='html')
            else:
                # keep the legacy test body if no html provided
                msg.set_content('Test depuis le serveur')

            s.send_message(msg)
            s.quit()
            print('OK_SENT')
            _write_email_log(f"SMTPLIB_DIRECT_SENT to={to_email} subject={msg['Subject']}")
        except Exception as e:
            _write_email_log(f"SMTPLIB_DIRECT_FAIL to={to_email} err={e}")

    t = threading.Thread(target=_worker)
    t.daemon = True
    t.start()
    

@require_POST
def parametres_update_profile(request):
    """AJAX endpoint: update the currently logged-in user's prenom and nom.

    quand on ajoute loption ajouter frais de paiment il faut qui les ajoute vers le frais restant a payer reele et les affiché dans le bon de paiment a imprimer  
    Expects POST params 'prenom' and 'nom'. Returns JSON { success: True, prenom, nom }
    or { success: False, error: '...' } on failure.
    """
    try:
        user_id = request.session.get('user_id')
        if not user_id:
            return JsonResponse({ 'success': False, 'error': 'Not authenticated' }, status=403)
        try:
            user = Utilisateur.objects.get(pk=user_id)
        except Utilisateur.DoesNotExist:
            return JsonResponse({ 'success': False, 'error': 'User not found' }, status=404)

        prenom = (request.POST.get('prenom') or '').strip()
        nom = (request.POST.get('nom') or '').strip()

        # Basic validation: require at least one non-empty name
        if not prenom and not nom:
            return JsonResponse({ 'success': False, 'error': 'Prénom ou nom requis' }, status=400)

        user.prenom = prenom
        user.nom = nom
        user.save()

        return JsonResponse({ 'success': True, 'prenom': user.prenom, 'nom': user.nom })
    except Exception as e:
        return JsonResponse({ 'success': False, 'error': str(e) }, status=500)


# Presence CRUD views
from django.views.generic import ListView, CreateView, UpdateView, DeleteView, DetailView
from django.urls import reverse_lazy


class PresenceListView(ListView):
    model = Presence
    template_name = 'presence_list.html'
    context_object_name = 'presences'
    paginate_by = 25


class PresenceCreateView(CreateView):
    model = Presence
    form_class = PresenceForm
    template_name = 'presence_form.html'
    success_url = reverse_lazy('presence_list')

    def get_initial(self):
        init = super().get_initial()
        # allow preselecting an event via GET ?event=
        ev = self.request.GET.get('event')
        if ev:
            try:
                init['calendar_event'] = CalendarEvent.objects.get(pk=int(ev))
            except Exception:
                pass
        return init

    def get_template_names(self):
        # return fragment when requested via AJAX so it can be loaded in a modal
        if self.request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return ['presence_form_fragment.html']
        return [self.template_name]

    def form_valid(self, form):
        # When AJAX, return JSON so frontend can close modal and reload
        resp = super().form_valid(form)
        if self.request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({'ok': True, 'pk': self.object.pk})
        return resp


class PresenceUpdateView(UpdateView):
    model = Presence
    form_class = PresenceForm
    template_name = 'presence_form.html'
    success_url = reverse_lazy('presence_list')

    def get_template_names(self):
        if self.request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return ['presence_form_fragment.html']
        return [self.template_name]

    def form_valid(self, form):
        resp = super().form_valid(form)
        if self.request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({'ok': True, 'pk': self.object.pk})
        return resp


class PresenceDetailView(DetailView):
    model = Presence
    template_name = 'presence_detail.html'
    context_object_name = 'presence'


class PresenceDeleteView(DeleteView):
    model = Presence
    template_name = 'presence_confirm_delete.html'
    success_url = reverse_lazy('presence_list')


def presence_delete_ajax(request, pk):
    """Delete a Presence via AJAX POST. Returns JSON."""
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'Method not allowed'}, status=405)
    try:
        p = Presence.objects.get(pk=pk)
        p.delete()
        return JsonResponse({'ok': True})
    except Presence.DoesNotExist:
        return JsonResponse({'ok': False, 'error': 'Not found'}, status=404)
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=500)


def api_search_etudiants(request):
    """Simple JSON endpoint to search students by name, id or telephone and return small results including photo URL."""
    q = request.GET.get('q', '').strip()
    results = []
    qs = None
    # Try parsing the query with common date formats (robustly) before falling back to text search
    from datetime import datetime
    parsed = None
    if q:
        tried = []
        # candidate formats to try
        fmts = ['%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%Y/%m/%d', '%Y-%m', '%Y/%m', '%Y']
        for f in fmts:
            try:
                dt = datetime.strptime(q.strip(), f)
                parsed = (f, dt)
                break
            except Exception:
                tried.append(f)
        try:
            if parsed:
                fmt, dt = parsed
                if fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%Y/%m/%d'):
                    qs = Etudiant.objects.filter(date_naissance=date(dt.year, dt.month, dt.day))[:200]
                elif fmt in ('%Y-%m', '%Y/%m'):
                    qs = Etudiant.objects.filter(date_naissance__year=dt.year, date_naissance__month=dt.month)[:400]
                elif fmt == '%Y':
                    qs = Etudiant.objects.filter(date_naissance__year=dt.year)[:400]
            else:
                qs = Etudiant.objects.filter(
                    Q(nom__icontains=q) | Q(prenom__icontains=q) | Q(id__icontains=q) | Q(telephone__icontains=q) | Q(mobile__icontains=q)
                )[:200]
        except Exception:
            qs = Etudiant.objects.none()
    else:
        qs = Etudiant.objects.all().order_by('-date_inscription')[:100]

    # Ensure we have related inscriptions available so we can determine the
    # real inscription status (avoid defaulting to 'non_inscrit' when the
    # Etudiant instance doesn't expose a precomputed attribute).
    try:
        from django.db.models import Prefetch
        qs = qs.prefetch_related(Prefetch('inscriptions', queryset=Inscription.objects.order_by('-date_inscription'), to_attr='inscriptions_prefetched'))
    except Exception:
        # If prefetch fails for any reason, continue — we'll fall back to per-row lookup below.
        pass

    # Convert QuerySet/slice into iterable and collect results
    for etu in qs:
        photo_url = ''
        if etu.photo:
            # If MEDIA_URL configured and photo is relative path
            from django.conf import settings
            try:
                photo_url = settings.MEDIA_URL + etu.photo
            except Exception:
                photo_url = etu.photo

        # try to compute remaining fees via shared util if available
        remaining = None
        try:
            balances = calculate_balances(etu.id)
            remaining = balances.get('ancien_solde')
        except Exception:
            remaining = None

        # Determine inscription status from prefetched inscriptions when available
        norm_status = 'non_inscrit'
        try:
            # Prefer prefetched list (avoids N+1). The Prefetch above stores results
            # on `inscriptions_prefetched` if available.
            ins_list = getattr(etu, 'inscriptions_prefetched', None)
            if ins_list is None:
                # fallback: query the DB for the latest inscription for this student
                latest_ins = Inscription.objects.filter(etudiant=etu).order_by('-date_inscription').first()
            else:
                latest_ins = ins_list[0] if len(ins_list) > 0 else None

            if latest_ins is not None:
                statut_val = (getattr(latest_ins, 'statut', '') or '')
                if 'inscrit' in str(statut_val).strip().lower() or 'actif' in str(statut_val).strip().lower():
                    norm_status = 'inscrit'
                else:
                    norm_status = 'non_inscrit'
            else:
                # as a last resort, check common Etudiant-level fields
                raw_status = (getattr(etu, 'inscription_status', '') or getattr(etu, 'statut', '') or '')
                raw_status_str = str(raw_status).strip().lower()
                if 'inscrit' in raw_status_str or 'actif' in raw_status_str:
                    norm_status = 'inscrit'
                else:
                    norm_status = 'non_inscrit'
        except Exception:
            norm_status = 'non_inscrit'

        results.append({
            'id': etu.id,
            'nom': etu.nom,
            'prenom': etu.prenom,
            'date_naissance': etu.date_naissance.isoformat() if getattr(etu, 'date_naissance', None) else '',
            'telephone': etu.telephone or etu.mobile or etu.phone or '',
            'email': etu.email or '',
            'address': etu.adresse or '',
            'remaining': str(remaining) if remaining is not None else '',
            'inscription_status': norm_status,
            'verification': getattr(etu, 'verification_step', 0),
            'formation': (etu.formation.id if getattr(etu, 'formation', None) else '') or (etu.formation_text or ''),
            'display': f"{etu.prenom} {etu.nom} (#{etu.id})",
            'photo': photo_url,
        })
    # Debug log to help with diagnosing queries (visible in server console)
    try:
        print(f"[api_search_etudiants] q={q!r} parsed={parsed!r} results={len(results)}")
    except Exception:
        pass

    return JsonResponse({'results': results})


def login_view(request):
    """Render login page and authenticate against Utilisateur model.

    Supports flexible login:
    - Email + Password
    - Nom (surname) + Password
    - ID Étudiant (alone, no password needed if matched)
    - Email/Nom + ID Étudiant (no password needed)
    
    Also supports account creation for students.
    """
    error = None
    success = None
    
    if request.method == 'POST':
        action = request.POST.get('action', 'login')
        
        if action == 'register':
            # Registration for students
            nom = request.POST.get('nom', '').strip()
            prenom = request.POST.get('prenom', '').strip()
            email = request.POST.get('email_register', '').strip()
            password = request.POST.get('password_register', '').strip()
            password_confirm = request.POST.get('password_confirm', '').strip()
            
            if not all([nom, prenom, email, password, password_confirm]):
                error = 'Tous les champs sont obligatoires.'
            elif password != password_confirm:
                error = 'Les mots de passe ne correspondent pas.'
            elif len(password) < 6:
                error = 'Le mot de passe doit contenir au moins 6 caractères.'
            elif Utilisateur.objects.filter(email__iexact=email).exists():
                error = 'Cet email est déjà utilisé.'
            else:
                try:
                    # Create new student user
                    user = Utilisateur.objects.create(
                        nom=nom,
                        prenom=prenom,
                        email=email,
                        mot_de_passe=password,  # In production, use hashing
                        role='etudiant',
                        statut='actif',
                        etat_compte='actif',
                        date_creation=datetime.utcnow()
                    )
                    # Create associated Etudiant profile
                    Etudiant.objects.create(
                        nom=nom,
                        prenom=prenom,
                        email=email,
                        date_inscription=date.today(),
                        statut='inscrit'
                    )
                    success = 'Compte créé avec succès! Vous pouvez maintenant vous connecter.'
                except Exception as e:
                    error = f'Erreur lors de la création du compte: {str(e)}'
        else:
            # Login
            identifier = request.POST.get('identifier', '').strip()
            password = request.POST.get('password', '').strip()
            
            try:
                user = None
                
                # Try to parse identifier as student ID
                student_id = None
                try:
                    student_id = int(identifier)
                except ValueError:
                    pass
                
                # METHOD 1: Try ID Étudiant (with or without password)
                if student_id:
                    try:
                        etudiant = Etudiant.objects.get(id=student_id)
                        # Find associated Utilisateur by email
                        if etudiant.email:
                            user = Utilisateur.objects.filter(email__iexact=etudiant.email).first()
                        
                        # If no user found by email, try by nom
                        if not user and etudiant.nom:
                            user = Utilisateur.objects.filter(nom__iexact=etudiant.nom).first()
                        
                        # If user found and password is provided, verify it
                        if user and password:
                            if not user.mot_de_passe or user.mot_de_passe != password:
                                user = None
                        # If no password provided, we still authenticate (ID only)
                        
                    except Etudiant.DoesNotExist:
                        user = None
                
                # METHOD 2: Try Email/Nom + Password (if no user found yet or if student_id wasn't valid)
                if not user and identifier and password:
                    user = Utilisateur.objects.filter(
                        Q(email__iexact=identifier) | Q(nom__iexact=identifier)
                    ).first()
                    
                    if user and (not user.mot_de_passe or user.mot_de_passe != password):
                        user = None
                
                # Authenticate if user found
                if user:
                    request.session['user_id'] = user.id
                    user.derniere_connexion = datetime.utcnow()
                    try:
                        user.save()
                    except Exception:
                        pass
                    return redirect('dashboard')
                else:
                    error = 'Identifiant invalide ou mot de passe incorrect. Veuillez réessayer.'
                    
            except Exception as e:
                error = f'Erreur lors de l\'authentification: {str(e)}'

    return render(request, 'login.html', {'error': error, 'success': success})

def logout_view(request):
    try:
        request.session.pop('user_id', None)
    except Exception:
        pass
    return redirect('login')

    thread = threading.Thread(target=_start)
    thread.daemon = True
    thread.start()


# Helper to read/write the student's 'situation' field flexibly.
def _get_situation(obj):
    """Return the situation value supporting multiple historical field names."""
    return (getattr(obj, 'situation', None)
            or getattr(obj, 'situation_professionnelle', None)
            or getattr(obj, 'situationprofessionnelle', None)
            or '')


def _set_situation(obj, value):
    """Set the situation value on the object using whatever writable field exists."""
    if hasattr(obj, 'situation'):
        setattr(obj, 'situation', value)
    elif hasattr(obj, 'situation_professionnelle'):
        setattr(obj, 'situation_professionnelle', value)
    else:
        # best-effort: create attribute if model missing the column
        setattr(obj, 'situation', value)







                               ####### Dashboard page ##################
def dashboard(request):
    # Check if user is logged in
    user_id = request.session.get('user_id')
    if not user_id:
        return redirect('login')
    
    # Get current user
    try:
        user = Utilisateur.objects.get(id=user_id)
    except:
        return redirect('login')
    
    # If student, show student dashboard
    if user.role == 'etudiant':
        # Get student info from Etudiant table
        try:
            etudiant = Etudiant.objects.get(utilisateur_id=user_id)
        except:
            # Try to find by email or nom
            try:
                etudiant = Etudiant.objects.filter(Q(email=user.email) | Q(nom=user.nom)).first()
            except:
                etudiant = None
        
        # Get inscriptions for this student
        inscriptions = Inscription.objects.filter(etudiant=etudiant) if etudiant else []
        
        # Get paiements for this student
        paiements = Paiement.objects.filter(etudiant=etudiant) if etudiant else []
        
        # Calculate totals
        total_formations = len(inscriptions)
        total_paye = paiements.filter(statut='payé').aggregate(total=Sum('montant'))['total'] or 0
        
        # Calculate total à payer from inscriptions
        total_a_payer = 0
        for inscription in inscriptions:
            if inscription.formation:
                # Use prix_etudiant if available, else use prix_total from inscription
                prix = inscription.formation.prix_etudiant or inscription.prix_total or 0
                total_a_payer += prix
        
        total_reste = total_a_payer - total_paye
        if total_reste < 0:
            total_reste = 0
        
        context = {
            'user': user,
            'etudiant': etudiant,
            'inscriptions': inscriptions,
            'paiements': paiements,
            'total_formations': total_formations,
            'total_paye': total_paye,
            'total_reste': total_reste,
        }
        
        return render(request, 'dashboard_etudiant.html', context)
    
    # Otherwise show admin dashboard
    nb_etudiants = Etudiant.objects.count()
    nb_formations = Formation.objects.count()
    nb_utilisateurs = Utilisateur.objects.count()





    paiements_qs = Paiement.objects.all()
    nb_paiements = paiements_qs.count()
    total_paiements = paiements_qs.aggregate(total=Sum('montant'))['total'] or 0
    # formations new in last 30 days if date_creation exists
    try:
        from django.utils import timezone
        cutoff = timezone.now() - timezone.timedelta(days=30)
        formations_new = Formation.objects.filter(date_creation__gte=cutoff).count()
    except Exception:
        formations_new = 0

    # formateurs: try to infer from Utilisateur.statut, else from availabilities
    try:
        nb_formateurs = Utilisateur.objects.filter(statut__icontains='formateur').count()
        if nb_formateurs == 0:
            nb_formateurs = AvailabilityFormateur.objects.values('utilisateur').distinct().count()
    except Exception:
        nb_formateurs = 0

    # availability percent: number of availability entries with times / max(1, users)
    try:
        avail_with_time = AvailabilityFormateur.objects.filter(start_time__isnull=False).count()
        total_users = max(1, Utilisateur.objects.count())
        availability_percent = int((avail_with_time / total_users) * 100) if total_users else 0
    except Exception:
        availability_percent = 0

    # tests active: use Module count as proxy
    try:
        tests_active = Module.objects.count()
    except Exception:
        tests_active = 0

    # success rate: average score from ProgressionModule
    try:
        avg_score = ProgressionModule.objects.aggregate(avg=Avg('score'))['avg'] or 0
        success_rate = int(avg_score)
    except Exception:
        success_rate = 0

    # salles and live occupation (use CalendarEvent currently running to compute live occupancy)
    try:
        from django.utils import timezone as dj_timezone
        now = dj_timezone.now()
        salle_count = Salle.objects.count()
        # Count distinct salles occupied by either CalendarEvent (salle_id) or Session (salle FK)
        try:
            start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
            end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=999999)

            # IDs from CalendarEvent overlapping 'now'
            try:
                cal_now_ids = set(CalendarEvent.objects.filter(
                    salle_id__isnull=False,
                    start_datetime__lte=now,
                    end_datetime__gte=now,
                ).values_list('salle_id', flat=True).distinct())
            except Exception:
                cal_now_ids = set()

            # IDs from Session overlapping 'now' (date-based fallback)
            try:
                sess_now_ids = set(Session.objects.filter(
                    salle__isnull=False,
                    date_debut__lte=now.date(),
                    date_fin__gte=now.date()
                ).values_list('salle', flat=True).distinct())
            except Exception:
                sess_now_ids = set()

            # Only count salle IDs that exist in the Salle table
            existing_salle_ids = set(Salle.objects.values_list('id', flat=True))
            combined_now_ids = (set([int(x) for x in (cal_now_ids or set()) if x is not None]) | set([int(x) for x in (sess_now_ids or set()) if x is not None])) & existing_salle_ids
            salle_occupied_count = len(combined_now_ids)

            # IDs from CalendarEvent overlapping today
            try:
                cal_today_ids = set(CalendarEvent.objects.filter(
                    salle_id__isnull=False,
                    start_datetime__lte=end_of_day,
                    end_datetime__gte=start_of_day,
                ).values_list('salle_id', flat=True).distinct())
            except Exception:
                cal_today_ids = set()

            # IDs from Session overlapping today
            try:
                sess_today_ids = set(Session.objects.filter(
                    salle__isnull=False,
                    date_debut__lte=now.date(),
                    date_fin__gte=now.date()
                ).values_list('salle', flat=True).distinct())
            except Exception:
                sess_today_ids = set()

            combined_today_ids = (set([int(x) for x in (cal_today_ids or set()) if x is not None]) | set([int(x) for x in (sess_today_ids or set()) if x is not None])) & existing_salle_ids
            salle_occupied_today_count = len(combined_today_ids)

            salle_occupation_percent = int((salle_occupied_count / max(1, salle_count)) * 100) if salle_count else 0
            salle_occupation_today_percent = int((salle_occupied_today_count / max(1, salle_count)) * 100) if salle_count else 0
        except Exception:
            salle_count = 0
            salle_occupied_count = 0
            salle_occupation_percent = 0
            salle_occupied_today_count = 0
            salle_occupation_today_percent = 0
    except Exception:
        salle_count = 0
        salle_occupied_count = 0
        salle_occupation_percent = 0

    # paid percent: invoices marked as paid
    try:
        invoices_qs = Invoice.objects.all()
        invoices_total = invoices_qs.count()
        invoices_paid = invoices_qs.filter(statut__icontains='pay').count() if invoices_total else 0
        paid_percent = int((invoices_paid / max(1, invoices_total)) * 100) if invoices_total else 0
    except Exception:
        paid_percent = 0

    # reports count: use invoices as proxy
    try:
        reports_count = Invoice.objects.count()
    except Exception:
        reports_count = 0
    # --- New metrics requested ---
    try:
        # total enrolled students (has any inscription)
        total_enrolled = Etudiant.objects.filter(inscriptions__isnull=False).distinct().count()
        # gender split among enrolled
        female_count = Etudiant.objects.filter(inscriptions__isnull=False, sexe__iexact='F').distinct().count()
        male_count = Etudiant.objects.filter(inscriptions__isnull=False, sexe__iexact='M').distinct().count()
        # situation counts: students (situation contains 'etudiant') and fonctionnaires
        situation_etudiant_count = Etudiant.objects.filter(inscriptions__isnull=False, situation_professionnelle__icontains='etudiant').distinct().count()
        fonctionnaire_count = Etudiant.objects.filter(inscriptions__isnull=False, situation_professionnelle__icontains='fonctionnaire').distinct().count()
    except Exception:
        total_enrolled = nb_etudiants
        female_count = 0
        male_count = 0
        situation_etudiant_count = 0
        fonctionnaire_count = 0

    try:
        # distribution by formation: counts and percent
        formation_counts = Formation.objects.annotate(num=Count('inscriptions')).order_by('-num')
        form_distribution = []
        total_ins = Inscription.objects.count() or 1
        for f in formation_counts:
            pct = int((f.num / total_ins) * 100) if total_ins else 0
            pct_str = f"{pct}%"
            form_distribution.append({'nom': f.nom, 'count': f.num, 'pct': pct, 'pct_str': pct_str})
    except Exception:
        form_distribution = []

    try:
        # distribution by niveau (students' level)
        lvl_qs = Etudiant.objects.values('niveau_etude').annotate(count=Count('id')).order_by('-count')
        total_lvl = sum([l['count'] for l in lvl_qs]) or 1
        levels_distribution = []
        for l in lvl_qs:
            name = l['niveau_etude'] or 'Non spécifié'
            cnt = l['count']
            pct = int((cnt / total_lvl) * 100) if total_lvl else 0
            pct_str = f"{pct}%"
            levels_distribution.append({'nom': name, 'count': cnt, 'pct': pct, 'pct_str': pct_str})
    except Exception:
        levels_distribution = []

    try:
        # new students this month
        from django.utils import timezone
        now = timezone.now()
        month_start = now.replace(day=1)
        new_this_month = Etudiant.objects.filter(date_inscription__gte=month_start).count()
    except Exception:
        new_this_month = 0

    try:
        # certifications count total and per formation
        cert_total = Certification.objects.count()
        certs_by_formation = {}
        for c in Certification.objects.select_related('inscription__formation'):
            f = c.inscription.formation.nom if c.inscription and c.inscription.formation else 'Autres'
            certs_by_formation[f] = certs_by_formation.get(f, 0) + 1
        # convert to list of tuples for template iteration
        certs_by_formation = sorted(certs_by_formation.items(), key=lambda x: -x[1])
    except Exception:
        cert_total = 0
        certs_by_formation = {}





    try:
        # finances: total revenue and revenue per formation
        total_revenue = Paiement.objects.aggregate(total=Sum('montant'))['total'] or 0
        # build a map of formation name -> total revenue from Paiement
        rev_map = {}
        for r in Paiement.objects.values('formation').annotate(total=Sum('montant')):
            fid = r.get('formation')
            rev_map[fid] = r.get('total') or 0
        # ensure every Formation appears, even if zero revenue
        revenue_by_formation = []
        for f in Formation.objects.all():
            total = rev_map.get(f.id, 0)
            revenue_by_formation.append({'formation': f.nom, 'total': total})
        # sort by revenue desc
        revenue_by_formation.sort(key=lambda x: (x['total'] is None, - (x['total'] or 0)))
    except Exception:
        total_revenue = 0
        revenue_by_formation = []

    try:
        # invoices paid vs pending
        inv_total = Invoice.objects.count()
        inv_paid = Invoice.objects.filter(statut__icontains='pay').count() if inv_total else 0
        inv_pct = int((inv_paid / inv_total) * 100) if inv_total else 0
        inv_pct_str = f"{inv_pct}%"
    except Exception:
        inv_total = 0
    inv_paid = 0
    inv_pct = 0
    inv_pct_str = "0%"

    # Groupes & Occupation (tous groupes de toutes formations)
    try:
        GROUP_CAPACITY = 16
        groups_qs = Inscription.objects.values_list('groupe', flat=True).distinct()
        group_codes = [g for g in groups_qs if g]
        group_count = len(group_codes)
        total_inscriptions = Inscription.objects.count() or 0
        possible_capacity = max(1, group_count * GROUP_CAPACITY)
        group_occupation_percent = int((total_inscriptions / possible_capacity) * 100) if group_count else 0
    except Exception:
        group_count = 0
        group_occupation_percent = 0

    # Nombre de paiements étudiants
    try:
        student_payment_count = Paiement.objects.filter(etudiant__isnull=False).count()
    except Exception:
        student_payment_count = 0

    try:
        total_inscriptions = Inscription.objects.count() or 0
        payments_percent = int((student_payment_count / total_inscriptions) * 100) if total_inscriptions else 0
        payments_pct_str = f"{payments_percent}%"
    except Exception:
        total_inscriptions = 0
        payments_percent = 0
        payments_pct_str = "0%"

    # Rapports imprimés (exemple: nombre d'invoices avec statut 'imprimé')
    try:
        printed_reports_count = Invoice.objects.filter(statut__icontains='imprim').count()
    except Exception:
        printed_reports_count = 0

    # Net profit mois/annuel
    from django.utils import timezone
    now = timezone.now()
    month_start = now.replace(day=1)
    year_start = now.replace(month=1, day=1)
    # day range for local 'today' (midnight -> next midnight)
    try:
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = (day_start + timezone.timedelta(days=1))
    except Exception:
        day_start = None
        day_end = None
    try:
        net_profit_month = Paiement.objects.filter(date_paiement__gte=month_start).aggregate(total=Sum('montant'))['total'] or 0
        net_profit_year = Paiement.objects.filter(date_paiement__gte=year_start).aggregate(total=Sum('montant'))['total'] or 0
        # compute daily profit (today)
        if day_start and day_end:
            net_profit_day = Paiement.objects.filter(date_paiement__gte=day_start, date_paiement__lt=day_end).aggregate(total=Sum('montant'))['total'] or 0
        else:
            net_profit_day = 0
    except Exception:
        net_profit_month = 0
        net_profit_year = 0
        net_profit_day = 0
    # compute trésorerie similarly to finances view: paiements - (reglements enseignants + fournisseurs)
    try:
        from .models import ReglementEnseignants
        reglements_enseignants_total = ReglementEnseignants.objects.aggregate(total=Sum('montant'))['total'] or 0
    except Exception:
        reglements_enseignants_total = 0
    fournisseurs_total = 0
    try:
        from .models import Charge
        charges_total = Charge.objects.aggregate(total=Sum('montant'))['total'] or 0
    except Exception:
        charges_total = 0
    try:
        tresorerie = total_paiements - (reglements_enseignants_total + charges_total)
    except Exception:
        tresorerie = 0

    # minimal role check: detect if current user is 'Secretaire' to adjust UI
    try:
        uid = request.session.get('user_id') or None
        current_user = Utilisateur.objects.filter(id=uid).first() if uid else None
        role = (getattr(current_user, 'role', '') or '').strip().lower() if current_user else ''
        is_secretaire = role in ('secretaire', 'secrétaire')
    except Exception:
        is_secretaire = False

    return render(request, 'dashboard.html', {
        'nb_etudiants': nb_etudiants,
        'nb_formations': nb_formations,
        'nb_utilisateurs': nb_utilisateurs,
        'nb_paiements': nb_paiements,
        'total_paiements': total_paiements,
        'formations_new': formations_new,
        'nb_formateurs': nb_formateurs,
        'availability_percent': availability_percent,
        'tests_active': tests_active,
        'success_rate': success_rate,
    'salle_count': salle_count,
    'salle_occupied_count': salle_occupied_count,
    'salle_occupation_percent': salle_occupation_percent,
        'paid_percent': paid_percent,
        'reports_count': reports_count,
        'formations': Formation.objects.all(),
        'utilisateurs': Utilisateur.objects.all(),
    # new dashboard metrics
    'total_enrolled': total_enrolled,
    'female_count': female_count,
    'male_count': male_count,
    'situation_etudiant_count': situation_etudiant_count,
    'fonctionnaire_count': fonctionnaire_count,
    'form_distribution': form_distribution,
    'new_this_month': new_this_month,
    'cert_total': cert_total,
    'certs_by_formation': certs_by_formation,
    'levels_distribution': levels_distribution,
    'total_revenue': total_revenue,
    'revenue_by_formation': revenue_by_formation,
    'group_count': group_count,
    'group_occupation_percent': group_occupation_percent,
    'student_payment_count': student_payment_count,
    'total_inscriptions': total_inscriptions,
    'payments_percent': payments_percent,
    'payments_pct_str': payments_pct_str,
    'printed_reports_count': printed_reports_count,
    'salle_occupied_today_count': salle_occupied_today_count,
    'salle_occupation_today_percent': salle_occupation_today_percent,
    'net_profit_month': net_profit_month,
    'net_profit_year': net_profit_year,
    'net_profit_day': net_profit_day,
    'tresorerie': tresorerie,
    'inv_total': inv_total,
    'inv_paid': inv_paid,
    'inv_pct': inv_pct,
    'inv_pct_str': inv_pct_str,
    'is_secretaire': is_secretaire,
    })



# Small JSON API used by the dashboard frontend
def dashboard_stats(request):
    nb_etudiants = Etudiant.objects.count()
    nb_formations = Formation.objects.count()
    nb_utilisateurs = Utilisateur.objects.count()

    paiements_qs = Paiement.objects.all()
    nb_paiements = paiements_qs.count()
    total = paiements_qs.aggregate(total=Sum('montant'))['total'] or 0

    # keep same metrics for API
    recent = []
    media_url = getattr(settings, 'MEDIA_URL', '') or ''
    # Only include students who registered since the start of the current month
    try:
        from django.utils import timezone
        now = timezone.now()
        month_start = now.replace(day=1)
        etu_qs = Etudiant.objects.filter(date_inscription__gte=month_start).order_by('-date_inscription')[:8]
        # fallback: if none for this month, show last 8 students
        if not etu_qs.exists():
            etu_qs = Etudiant.objects.order_by('-id')[:8]
    except Exception:
        etu_qs = Etudiant.objects.order_by('-id')[:8]

    for e in etu_qs:
        photo = ''
        if getattr(e, 'photo', None):
            # assume photo stores path relative to MEDIA_ROOT
            photo = media_url + e.photo.lstrip('/')
        # include date_inscription so frontend can show/hint if needed
        recent.append({'id': e.id, 'nom': e.nom, 'prenom': e.prenom, 'photo': photo, 'niveau_etude': e.niveau_etude, 'date_inscription': getattr(e, 'date_inscription', None)})

    return JsonResponse({
        'success': True,
        'nb_etudiants': nb_etudiants,
        'nb_formations': nb_formations,
        'nb_utilisateurs': nb_utilisateurs,
        'nb_paiements': nb_paiements,
        'total_paiements': str(total),
        'recent_etudiants': recent,
    })


@require_POST
def presence_clock_in(request):
    """Clock-in (pointeuse) endpoint.
    Expects POST form-data or JSON with 'code' field (student code or id).
    Finds the Etudiant by id, nin or telephone and assigns a Presence for today's
    closest CalendarEvent. Returns JSON { ok: True, pk: <presence pk>, message: '...' }
    """
    try:
        # accept JSON body or form-encoded
        data = {}
        try:
            data = json.loads(request.body.decode('utf-8') or '{}')
        except Exception:
            data = request.POST.dict()
        code = (data.get('code') or data.get('q') or '').strip()
        if not code:
            return JsonResponse({'ok': False, 'error': 'Code requis'}, status=400)

        # find student heuristic: try numeric id, then nin (exact), then telephone contains
        etu = None
        try:
            if code.isdigit():
                etu = Etudiant.objects.filter(pk=int(code)).first()
        except Exception:
            etu = None
        if not etu:
            etu = Etudiant.objects.filter(nin__iexact=code).first()
        if not etu:
            etu = Etudiant.objects.filter(Q(telephone__icontains=code) | Q(mobile__icontains=code)).first()
        if not etu:
            return JsonResponse({'ok': False, 'error': 'Étudiant introuvable pour le code fourni'}, status=404)

        # find today's events for the student's formation(s) and pick the one whose start_datetime is closest to now
        from django.utils import timezone as dj_tz
        now = dj_tz.now()
        today = now.date()

        # collect formation ids the student is enrolled in (inscriptions) and the Etudiant.formation FK if present
        formation_ids = set()
        try:
            if getattr(etu, 'formation', None):
                formation_ids.add(int(etu.formation.id))
        except Exception:
            pass
        try:
            ins_fids = list(etu.inscriptions.values_list('formation_id', flat=True))
            for fid in ins_fids:
                if fid:
                    formation_ids.add(int(fid))
        except Exception:
            pass

        # Query events for today that match one of the student's formations
        events = CalendarEvent.objects.filter(start_datetime__date=today)
        if formation_ids:
            events = events.filter(formation_id__in=list(formation_ids))
        else:
            # If we couldn't determine formation ids, attempt to match by textual formation name
            fname = (getattr(etu, 'formation_text', '') or (getattr(etu, 'formation', None) and getattr(etu.formation, 'nom', '')) or '')
            if fname:
                events = events.filter(formation_name__icontains=fname)

        events = events.order_by('start_datetime')
        if not events.exists():
            return JsonResponse({'ok': False, 'error': "Aucune séance de la formation de l'étudiant aujourd'hui"}, status=404)

        # choose minimal absolute time difference
        best = None
        best_diff = None
        for ev in events:
            try:
                ev_dt = ev.start_datetime
                diff = abs((ev_dt - now).total_seconds())
                if best is None or diff < best_diff:
                    best = ev
                    best_diff = diff
            except Exception:
                continue

        if not best:
            return JsonResponse({'ok': False, 'error': 'Séance introuvable'}, status=404)

        # avoid duplicate presence for same student/event
        existing = Presence.objects.filter(etudiant=etu, calendar_event=best).first()
        if existing:
            return JsonResponse({'ok': True, 'pk': existing.pk, 'message': 'Présence déjà enregistrée'} )

        # create presence entry (leave statut to default if model provides one)
        p = Presence.objects.create(etudiant=etu, calendar_event=best)
        return JsonResponse({'ok': True, 'pk': p.pk, 'message': 'Présence enregistrée'})
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=500)


def parametres_view(request):
    """Minimal settings page placeholder."""
    # show utilisateurs to the template for user management
    from types import SimpleNamespace
    from django.db import DatabaseError
    try:
        # Normal path: return ORM queryset (works when DB schema matches model)
        utilisateurs = Utilisateur.objects.all().order_by('id')
    except DatabaseError:
        # Fallback: the DB table may be missing the 'role' column (legacy schema).
        # Query only a safe subset of columns and create simple objects for the template.
        try:
            rows = list(Utilisateur.objects.values('id', 'email', 'mot_de_passe').order_by('id'))
            utilisateurs = []
            for r in rows:
                # ensure keys expected by template exist
                obj = SimpleNamespace(id=r.get('id'), email=r.get('email'), mot_de_passe=r.get('mot_de_passe') or '', role='')
                utilisateurs.append(obj)
        except Exception:
            utilisateurs = []

    # load simple school info (name + logo) from a JSON file if present
    school_name = ''
    school_logo = ''
    try:
        from django.conf import settings
        import json, os
        info_path = os.path.join(settings.BASE_DIR, 'school_info.json')
        if os.path.exists(info_path):
            with open(info_path, 'r', encoding='utf-8') as f:
                info = json.load(f)
                school_name = info.get('name', '')
                school_logo = info.get('logo', '')
    except Exception:
        school_name = ''
        school_logo = ''

    # determine if current user is Secretaire to adjust UI
    try:
        uid = request.session.get('user_id') or None
        current_user = Utilisateur.objects.filter(id=uid).first() if uid else None
        role = (getattr(current_user, 'role', '') or '').strip().lower() if current_user else ''
        is_secretaire = role in ('secretaire', 'secrétaire')
    except Exception:
        is_secretaire = False

    return render(request, 'parametres.html', {'utilisateurs': utilisateurs, 'school_name': school_name, 'school_logo': school_logo, 'is_secretaire': is_secretaire, 'current_user': current_user})


def parametres_update_school(request):
    """Accept POST to update the school's name and optional logo file.

    Saves uploaded logo under MEDIA_ROOT/school_logo/ and writes a small JSON file
    `school_info.json` in BASE_DIR to persist the name and logo path.
    """
    # ...existing code continues


def parametres_set_database(request):
    """AJAX endpoint: set the active database name in the user's session.

    Expects POST with 'db' equal to one of the keys present in settings.DATABASES.
    Returns JSON {success: True} on success or {success: False, error: '...'}.
    """
    from django.views.decorators.http import require_POST
    from django.conf import settings as djsettings
    @require_POST
    def _inner(req):
        name = req.POST.get('db') or req.body.decode('utf-8') if hasattr(req, 'body') else None
        if not name:
            return JsonResponse({'success': False, 'error': 'no_db_provided'}, status=400)
        # basic validation: must be a configured database
        if name not in djsettings.DATABASES:
            return JsonResponse({'success': False, 'error': 'unknown_db'}, status=400)
        try:
            req.session['active_db'] = name
            return JsonResponse({'success': True})
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)}, status=500)
    return _inner(request)
    from django.views.decorators.http import require_POST
    from django.conf import settings
    import os, json

    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)

    name = (request.POST.get('school_name') or '').strip()
    logo_file = request.FILES.get('school_logo')

    try:
        logo_path = ''
        if logo_file:
            # ensure directory
            dest_dir = os.path.join(settings.MEDIA_ROOT, 'school_logo')
            os.makedirs(dest_dir, exist_ok=True)
            filename = logo_file.name
            safe_path = os.path.join(dest_dir, filename)
            # write file
            with open(safe_path, 'wb') as out:
                for chunk in logo_file.chunks():
                    out.write(chunk)
            # build web-friendly URL using forward slashes (MEDIA_URL may be '/media/')
            logo_path = settings.MEDIA_URL.rstrip('/') + '/school_logo/' + filename

        # persist info
        info_path = os.path.join(settings.BASE_DIR, 'school_info.json')
        info = {}
        if os.path.exists(info_path):
            try:
                with open(info_path, 'r', encoding='utf-8') as f:
                    info = json.load(f)
            except Exception:
                info = {}
        if name:
            info['name'] = name
        if logo_path:
            info['logo'] = logo_path
        with open(info_path, 'w', encoding='utf-8') as f:
            json.dump(info, f)

        return JsonResponse({'success': True, 'name': info.get('name', ''), 'logo': info.get('logo', '')})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


def _is_admin_request(request):
    # Very small helper: determine if current session user is admin using session 'user_id' mapping
    try:
        uid = request.session.get('user_id') or request.POST.get('current_user_id') or None
        if not uid:
            return False
        u = Utilisateur.objects.filter(id=uid).first()
        if not u:
            return False
        return (getattr(u, 'role', '') or '').lower() == 'admin'
    except Exception:
        return False


def users_upsert(request):
    # Create or update a Utilisateur. Accessible to Admins only.
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Invalid method'}, status=405)
    try:
        uid = request.POST.get('id') or None
        email = (request.POST.get('email') or '').strip()
        nom = (request.POST.get('nom') or '').strip() or None
        role = (request.POST.get('role') or '').strip() or None
        password = request.POST.get('password') or None
        if not email:
            return JsonResponse({'success': False, 'error': 'Missing email'}, status=400)
        # accept role as a plain attribute (no server-side role whitelist)
        # role may be None or any string; assign directly below
        # create
        if not uid:
            if Utilisateur.objects.filter(email__iexact=email).exists():
                return JsonResponse({'success': False, 'error': 'Email déjà utilisé'}, status=400)
            u = Utilisateur()
            u.email = email
            u.role = role
            if nom is not None:
                u.nom = nom
            # store password exactly as provided (per user request: simple CRUD, no hashing)
            if password is not None:
                u.mot_de_passe = password
            u.save()
            return JsonResponse({'success': True, 'user': {'id': u.id, 'email': u.email, 'nom': u.nom, 'role': u.role, 'password': u.mot_de_passe}})
        # update
        else:
            u = Utilisateur.objects.filter(id=uid).first()
            if not u:
                return JsonResponse({'success': False, 'error': 'Utilisateur introuvable'}, status=404)
            # prevent email collision
            if u.email.lower() != email.lower() and Utilisateur.objects.filter(email__iexact=email).exclude(id=u.id).exists():
                return JsonResponse({'success': False, 'error': 'Email déjà utilisé par un autre'}, status=400)
            u.email = email
            if nom is not None:
                u.nom = nom
            if role is not None:
                u.role = role
            # set password to exactly what was provided (including empty string)
            if password is not None:
                u.mot_de_passe = password
            u.save()
            return JsonResponse({'success': True, 'user': {'id': u.id, 'email': u.email, 'nom': u.nom, 'role': u.role, 'password': u.mot_de_passe}})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


def users_delete(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Invalid method'}, status=405)
    try:
        uid = request.POST.get('id') or request.POST.get('_id')
        if not uid:
            return JsonResponse({'success': False, 'error': 'Missing id'}, status=400)
        u = Utilisateur.objects.filter(id=uid).first()
        if not u:
            return JsonResponse({'success': False, 'error': 'Not found'}, status=404)
        u.delete()
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)














# Placeholder pages for dashboard cards (may be empty initially)
def etudiants_view(request):
    # Render list of students and formations for the etudiants page.
    # Prefetch inscriptions and paginate the result. Each page's students get
    # a computed `display_formation` attribute (FK > formation_text > latest inscription).
    from django.db.models import Prefetch

    # base queryset with prefetch to avoid N+1
    inscriptions_qs = Inscription.objects.select_related('formation').order_by('-date_inscription')
    paiements_qs = Paiement.objects.order_by('-date_paiement')
    qs = Etudiant.objects.select_related('formation').prefetch_related(
        Prefetch('inscriptions', queryset=inscriptions_qs, to_attr='inscriptions_prefetched'),
        Prefetch('paiements', queryset=paiements_qs, to_attr='paiements_prefetched')
    ).all().order_by('-id')

    # Filter by niveau_etude if provided
    current_niveau = request.GET.get('niveau')
    if current_niveau:
        qs = qs.filter(niveau_etude__iexact=current_niveau)

    # static niveau options (always show these three)
    niveau_options = ['Terminal', 'Universitaire', 'Diplomé']

    # No pagination: show all students on a single page
    students_list = list(qs)

    # Compute display_formation for every student
    for s in students_list:
        # determine displayed formation: FK > formation_text > latest inscription formation
        if s.formation:
            s.display_formation = s.formation.nom
            try:
                s.formation_id_for_filter = s.formation.id
            except Exception:
                s.formation_id_for_filter = ''
        elif getattr(s, 'formation_text', None):
            s.display_formation = s.formation_text
            s.formation_id_for_filter = ''
        else:
            ins_list = getattr(s, 'inscriptions_prefetched', [])
            if ins_list and getattr(ins_list[0], 'formation', None):
                s.display_formation = ins_list[0].formation.nom
                try:
                    s.formation_id_for_filter = ins_list[0].formation.id
                except Exception:
                    s.formation_id_for_filter = ''
            else:
                s.display_formation = ''
                s.formation_id_for_filter = ''
        # determine inscription status: 'inscrit' if there's an active inscription with statut 'inscrit'
        # use normalized value 'non_inscrit' by default for frontend compatibility
        s.inscription_status = 'non_inscrit'
        s.inscribed_formation = ''
        try:
            for ins in getattr(s, 'inscriptions_prefetched', []):
                if getattr(ins, 'statut', '').lower() == 'inscrit':
                    s.inscription_status = 'inscrit'
                    s.inscribed_formation = ins.formation.nom if getattr(ins, 'formation', None) else ''
                    break
        except Exception:
            pass
        # expose ecole if present on Etudiant model (fallback to empty string)
        try:
            s.ecole = getattr(s, 'ecole', '')
        except Exception:
            s.ecole = ''
        # compute total due from inscriptions and total paid from paiements
        total_due = Decimal('0.00')
        for ins in getattr(s, 'inscriptions_prefetched', []):
            try:
                if ins.prix_total:
                    total_due += Decimal(ins.prix_total)
            except Exception:
                continue
        total_paid = Decimal('0.00')
        for p in getattr(s, 'paiements_prefetched', []):
            try:
                if p.montant:
                    total_paid += Decimal(p.montant)
            except Exception:
                continue
        # keep signed remaining (can be negative to represent credit/overpayment)
        remaining = total_due - total_paid
        # Use shared calculate_balances util so the students list and the payments JSON
        # use the exact same computation (ordering, fallbacks, etc.). Fall back to
        # the naive calculation if the util fails for any reason. Also compute an
        # explicit `regle` boolean so the template can treat "réglé" as an
        # independent flag (balance exactly zero) rather than inferring from
        # positive/negative values.
        try:
            from .utils import calculate_balances
            balances = calculate_balances(s.id)
            val = balances.get('ancien_solde', remaining)
            s.remaining_fees = str(val)
            try:
                s.regle = abs(float(val or 0.0)) < 1e-6
            except Exception:
                s.regle = False
        except Exception:
            # fallback: use naive remaining value
            s.remaining_fees = str(remaining)
            try:
                s.regle = abs(float(remaining or 0.0)) < 1e-6
            except Exception:
                s.regle = False
        # Additionally consider an explicit 'paiement_regle' marker stored in
        # Etudiant.remarques or in any related Inscription.remarques. This avoids
        # adding a new DB column: the marker is stored as a token like
        # 'paiement_regle:1' inside the free-text remarques field (semicolon-separated).
        try:
            marked = False
            try:
                rm = (s.remarques or '')
                if 'paiement_regle:1' in rm:
                    marked = True
            except Exception:
                marked = False
            if not marked:
                for ins in getattr(s, 'inscriptions_prefetched', []):
                    try:
                        if ins and ins.remarques and 'paiement_regle:1' in ins.remarques:
                            marked = True
                            break
                    except Exception:
                        continue
            s.marked_regle = bool(marked)
            # expose a display value used by the template: show 'Réglé' when marked/zero
            if s.marked_regle or getattr(s, 'regle', False):
                s.remaining_display = 'Réglé'
            else:
                s.remaining_display = None
        except Exception:
            s.marked_regle = False
            s.remaining_display = None
        # Determine whether the student should be excluded from totals.
        # Exclude when the student has NO inscription for 'Genie Systeme' (case-insensitive).
        try:
            has_genie = False
            for ins in getattr(s, 'inscriptions_prefetched', []):
                try:
                    ec = (ins.ecole or '').strip().lower()
                    if ec == 'genie systeme':
                        has_genie = True
                        break
                except Exception:
                    continue
            s.exclude_from_totals = not bool(has_genie)
            if s.exclude_from_totals:
                # hide monetary remaining: treat as if not enrolled for totals and client-side sums
                s.original_remaining_fees = getattr(s, 'remaining_fees', '0')
                s.remaining_fees = '0'
                # ensure the template has a textual label for excluded students
                if s.marked_regle or getattr(s, 'regle', False):
                    s.remaining_display = 'Réglé'
                else:
                    s.remaining_display = 'Non réglé'
        except Exception:
            s.exclude_from_totals = False
        # compute photo url for template (MEDIA_URL + stored path) if present
        try:
            # use storage.url so the URL is correct for the configured storage backend
            s.photo_url = default_storage.url(s.photo) if s.photo else ''
        except Exception:
            try:
                s.photo_url = (settings.MEDIA_URL + s.photo) if s.photo else ''
            except Exception:
                s.photo_url = s.photo or ''
        # Normalize URL for browser: convert backslashes and ensure it begins with / or http
        try:
            if s.photo_url:
                s.photo_url = s.photo_url.replace('\\', '/')
                if not s.photo_url.startswith('http') and not s.photo_url.startswith('/'):
                    s.photo_url = (settings.MEDIA_URL or '/') + s.photo_url
        except Exception:
            pass

    formations = Formation.objects.all()
    # Compute total remaining to pay for the (possibly filtered) students list.
    # Requirement: sum all negative balances and subtract positive balances.
    # This is equivalent to: total_reste_a_payer = - sum(all remaining_fees values)
    total_reste_a_payer = Decimal('0.00')
    try:
        for s in students_list:
            # skip students explicitly excluded (inscriptions for other schools)
            try:
                if getattr(s, 'exclude_from_totals', False):
                    continue
            except Exception:
                pass
            try:
                val = Decimal(str(s.remaining_fees))
            except Exception:
                # if remaining_fees is not parseable, treat as zero
                val = Decimal('0.00')
            # subtract the student's balance: -(-400)=+400, -(+200)=-200
            total_reste_a_payer -= val
    except Exception:
        total_reste_a_payer = Decimal('0.00')
    # Provide formateurs list for inline inscription modal (use Enseignant if available, else Utilisateur with statut)
    try:
        formateurs_qs = Enseignant.objects.all()
        if not formateurs_qs.exists():
            formateurs_qs = Utilisateur.objects.filter(statut__icontains='formateur')
    except Exception:
        formateurs_qs = Utilisateur.objects.filter(statut__icontains='formateur')

    # build per-formation groupe options from actual Groupe rows (use groupe.capacite or default 16)
    import json
    formation_groups = {}
    DEFAULT_GROUP_CAPACITY = 16
    from .models import Groupe as GroupeModel
    for f in formations:
        opts = []
        # fetch groupes defined for this formation (ordered by created_at)
        try:
            groupes_qs = GroupeModel.objects.filter(formation=f).order_by('created_at')
        except Exception:
            groupes_qs = []
        # If there are defined groupes, use them; otherwise keep an empty list
        for g in groupes_qs:
            try:
                cap = int(g.capacite) if g.capacite is not None else DEFAULT_GROUP_CAPACITY
            except Exception:
                cap = DEFAULT_GROUP_CAPACITY
            try:
                cnt = Inscription.objects.filter(formation=f, groupe=g).count()
            except Exception:
                cnt = 0
            opts.append({'id': g.id, 'nom': g.nom, 'disabled': cnt >= cap, 'count': cnt, 'capacity': cap})
        formation_groups[str(f.id)] = opts

    return render(request, 'etudiants.html', {
        'count': len(students_list),
        'students': students_list,
        'total_reste_a_payer': total_reste_a_payer,
        'formations': formations,
        'paginator': None,
        'page_obj': None,
        'is_paginated': False,
        'per_page': None,
        'niveau_options': niveau_options,
        'current_niveau': current_niveau,
        'formateurs': formateurs_qs,
        'formation_groups_json': json.dumps(formation_groups),
    })


@require_POST
def toggle_etudiant_paiement(request, pk):
    """AJAX endpoint: toggle the 'paiement_regle' marker stored in Etudiant.remarques.

    This avoids adding a DB column: the endpoint inserts or removes the token
    'paiement_regle:1' in the `remarques` free-text field of the Etudiant.
    """
    try:
        etu = Etudiant.objects.filter(pk=pk).first()
        if not etu:
            return JsonResponse({'ok': False, 'error': 'Etudiant introuvable'}, status=404)
        rm = (etu.remarques or '')
        if 'paiement_regle:1' in rm:
            # remove token
            parts = [p.strip() for p in rm.split(';') if p.strip() and p.strip() != 'paiement_regle:1']
            etu.remarques = ';'.join(parts) if parts else None
            etu.save()
            new_state = False
        else:
            parts = [p.strip() for p in rm.split(';') if p.strip()]
            parts.append('paiement_regle:1')
            etu.remarques = ';'.join(parts)
            etu.save()
            new_state = True
        return JsonResponse({'ok': True, 'paiement_regle': new_state, 'etudiant_id': etu.id})
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)}, status=500)
def etudiants_export_xlsx(request):
    """Export students matching optional query params to XLSX. Uses openpyxl if installed."""
    try:
        import openpyxl
        from openpyxl.utils import get_column_letter
    except Exception:
        # Fallback to CSV if openpyxl is not available to ensure export still works
        qs = Etudiant.objects.select_related('formation').all().order_by('id')
        formation_ids = request.GET.getlist('formation')
        if formation_ids:
            try:
                ints = [int(x) for x in formation_ids if x]
                if ints:
                    qs = qs.filter(Q(formation__id__in=ints) | Q(formation_text__in=formation_ids))
            except Exception:
                qs = qs.filter(Q(formation__id__in=formation_ids) | Q(formation_text__in=formation_ids))

        import csv
        from io import StringIO
        sio = StringIO()
        writer = csv.writer(sio)
        # comprehensive columns
        headers = [
            'ID','Nom','Prenom','Nom (AR)','Prenom (AR)','Sexe','Date naissance','Lieu naissance','Nationalite','NIN',
            'Adresse','Telephone','Mobile','Email','Formation','Formation (texte)','Duree formation','Salle','Niveau',
            'Situation','Dernier diplome','Date inscription','Statut','Remarques','Verification step'
        ]
        writer.writerow(headers)
        for s in qs:
            formation = s.formation.nom if getattr(s, 'formation', None) else (s.formation_text or '')
            sit = _get_situation(s)
            row = [
                s.id,
                s.nom or '',
                s.prenom or '',
                s.nom_arabe or '',
                s.prenom_arabe or '',
                s.sexe or '',
                (s.date_naissance.isoformat() if getattr(s, 'date_naissance', None) else ''),
                s.lieu_naissance or '',
                s.nationalite or '',
                s.nin or '',
                s.adresse or '',
                s.telephone or '',
                s.mobile or '',
                s.email or '',
                formation,
                s.formation_text or '',
                s.duree_formation or '',
                s.salle or '',
                s.niveau_etude or '',
                sit or '',
                s.dernier_diplome or '',
                (s.date_inscription.isoformat() if getattr(s, 'date_inscription', None) else ''),
                s.statut or '',
                s.remarques or '',
                str(getattr(s, 'verification_step', '') or ''),
            ]
            writer.writerow([str(x) if x is not None else '' for x in row])
        resp = HttpResponse(sio.getvalue().encode('utf-8'), content_type='text/csv')
        resp['Content-Disposition'] = 'attachment; filename=etudiants_export.csv'
        return resp

    qs = Etudiant.objects.select_related('formation').all().order_by('id')
    # optional formation filter by id
    formation_ids = request.GET.getlist('formation')
    if formation_ids:
        # allow either ids or names; prefer numeric ids
        try:
            ints = [int(x) for x in formation_ids if x]
            if ints:
                qs = qs.filter(Q(formation__id__in=ints) | Q(formation_text__in=formation_ids))
        except Exception:
            qs = qs.filter(Q(formation__id__in=formation_ids) | Q(formation_text__in=formation_ids))

    # create workbook with styled header
    from openpyxl.utils import get_column_letter
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Etudiants'
    headers = [
        'ID','Nom','Prenom','Nom (AR)','Prenom (AR)','Sexe','Date naissance','Lieu naissance','Nationalite','NIN',
        'Adresse','Telephone','Mobile','Email','Formation','Formation (texte)','Duree formation','Salle','Niveau',
        'Situation','Dernier diplome','Date inscription','Statut','Remarques','Verification step'
    ]
    ws.append(headers)
    # header styling
    hdr_fill = openpyxl.styles.PatternFill(start_color='FFDCFCE6', end_color='FFDCFCE6', fill_type='solid')
    hdr_font = openpyxl.styles.Font(bold=True, color='FF064E3B')
    for col_idx, cell in enumerate(ws[1], 1):
        cell.fill = hdr_fill
        cell.font = hdr_font

    for s in qs:
        formation = s.formation.nom if getattr(s, 'formation', None) else (s.formation_text or '')
        sit = _get_situation(s)
        row = [
            s.id,
            s.nom or '',
            s.prenom or '',
            s.nom_arabe or '',
            s.prenom_arabe or '',
            s.sexe or '',
            (s.date_naissance.isoformat() if getattr(s, 'date_naissance', None) else ''),
            s.lieu_naissance or '',
            s.nationalite or '',
            s.nin or '',
            s.adresse or '',
            s.telephone or '',
            s.mobile or '',
            s.email or '',
            formation,
            s.formation_text or '',
            s.duree_formation or '',
            s.salle or '',
            s.niveau_etude or '',
            sit or '',
            s.dernier_diplome or '',
            (s.date_inscription.isoformat() if getattr(s, 'date_inscription', None) else ''),
            s.statut or '',
            s.remarques or '',
            str(getattr(s, 'verification_step', '') or ''),
        ]
        ws.append(row)

    # autosize columns
    for i, col in enumerate(ws.columns, 1):
        max_length = 0
        for cell in col:
            try:
                v = str(cell.value or '')
            except Exception:
                v = ''
            if len(v) > max_length:
                max_length = len(v)
        ws.column_dimensions[get_column_letter(i)].width = min(60, max_length + 2)

    from io import BytesIO
    output = BytesIO()
    wb.save(output)
    output.seek(0)
    resp = HttpResponse(output.read(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    resp['Content-Disposition'] = 'attachment; filename=etudiants_export.xlsx'
    return resp


def etudiants_export_pdf(request):
    """Export students to a simple PDF table using reportlab if available."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        from reportlab.lib import colors
        from reportlab.lib.styles import getSampleStyleSheet
    except Exception:
        # Fallback to CSV if reportlab is missing so users still get their data
        qs = Etudiant.objects.select_related('formation').all().order_by('id')
        formation_ids = request.GET.getlist('formation')
        if formation_ids:
            try:
                ints = [int(x) for x in formation_ids if x]
                if ints:
                    qs = qs.filter(Q(formation__id__in=ints) | Q(formation_text__in=formation_ids))
            except Exception:
                qs = qs.filter(Q(formation__id__in=formation_ids) | Q(formation_text__in=formation_ids))

        import csv
        from io import StringIO
        sio = StringIO()
        writer = csv.writer(sio)
        headers = [
            'ID','Nom','Prenom','Nom (AR)','Prenom (AR)','Sexe','Date naissance','Lieu naissance','Nationalite','NIN',
            'Adresse','Telephone','Mobile','Email','Formation','Formation (texte)','Duree formation','Salle','Niveau',
            'Situation','Dernier diplome','Date inscription','Statut','Remarques','Verification step'
        ]
        writer.writerow(headers)
        for s in qs:
            formation = s.formation.nom if getattr(s, 'formation', None) else (s.formation_text or '')
            sit = _get_situation(s)
            row = [
                s.id, s.nom or '', s.prenom or '', s.nom_arabe or '', s.prenom_arabe or '', s.sexe or '',
                (s.date_naissance.isoformat() if getattr(s, 'date_naissance', None) else ''), s.lieu_naissance or '',
                s.nationalite or '', s.nin or '', s.adresse or '', s.telephone or '', s.mobile or '', s.email or '',
                formation, s.formation_text or '', s.duree_formation or '', s.salle or '', s.niveau_etude or '',
                sit or '', s.dernier_diplome or '', (s.date_inscription.isoformat() if getattr(s, 'date_inscription', None) else ''),
                s.statut or '', s.remarques or '', str(getattr(s, 'verification_step', '') or '')
            ]
            writer.writerow([str(x) if x is not None else '' for x in row])
        resp = HttpResponse(sio.getvalue().encode('utf-8'), content_type='text/csv')
        resp['Content-Disposition'] = 'attachment; filename=etudiants_export.csv'
        return resp

    qs = Etudiant.objects.select_related('formation').all().order_by('id')
    formation_ids = request.GET.getlist('formation')
    if formation_ids:
        try:
            ints = [int(x) for x in formation_ids if x]
            if ints:
                qs = qs.filter(Q(formation__id__in=ints) | Q(formation_text__in=formation_ids))
        except Exception:
            qs = qs.filter(Q(formation__id__in=formation_ids) | Q(formation_text__in=formation_ids))

    # build full data table for PDF
    headers = [
        'ID','Nom','Prenom','Nom (AR)','Prenom (AR)','Sexe','Date naissance','Lieu naissance','Nationalite','NIN',
        'Adresse','Telephone','Mobile','Email','Formation','Formation (texte)','Duree formation','Salle','Niveau',
        'Situation','Dernier diplome','Date inscription','Statut','Remarques','Verification step'
    ]
    data = [headers]
    for s in qs:
        formation = s.formation.nom if getattr(s, 'formation', None) else (s.formation_text or '')
        sit = _get_situation(s)
        data.append([
            str(s.id), s.nom or '', s.prenom or '', s.nom_arabe or '', s.prenom_arabe or '', s.sexe or '',
            (s.date_naissance.isoformat() if getattr(s, 'date_naissance', None) else ''), s.lieu_naissance or '',
            s.nationalite or '', s.nin or '', s.adresse or '', s.telephone or '', s.mobile or '', s.email or '',
            formation, s.formation_text or '', s.duree_formation or '', s.salle or '', s.niveau_etude or '',
            sit or '', s.dernier_diplome or '', (s.date_inscription.isoformat() if getattr(s, 'date_inscription', None) else ''),
            s.statut or '', s.remarques or '', str(getattr(s, 'verification_step', '') or '')
        ])

    from io import BytesIO
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=24, rightMargin=24, topMargin=24, bottomMargin=24)
    styles = getSampleStyleSheet()
    elems = []
    elems.append(Paragraph('Etudiants export', styles['Heading2']))
    elems.append(Spacer(1, 12))
    table = Table(data, repeatRows=1, hAlign='LEFT')
    table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#DCFCE7')),
        ('TEXTCOLOR', (0,0), (-1,0), colors.HexColor('#064E3B')),
        ('GRID', (0,0), (-1,-1), 0.4, colors.HexColor('#9CA3AF')),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,0), 9),
        ('FONTSIZE', (0,1), (-1,-1), 8),
    ]))
    elems.append(table)
    doc.build(elems)
    buffer.seek(0)
    resp = HttpResponse(buffer.read(), content_type='application/pdf')
    resp['Content-Disposition'] = 'attachment; filename=etudiants_export.pdf'
    return resp


def _parse_date(value):
    """Parse a date-like value from various common formats to a date object.

    Accepts date/datetime objects (returns date), ISO strings, or 'DD/MM/YYYY' and 'YYYY-MM-DD'.
    Returns None if parsing fails or value is falsy.
    """
    if not value:
        return None
    # already a date/datetime
    try:
        if isinstance(value, date):
            return value
    except Exception:
        pass
    # strings
    s = str(value).strip()
    if not s:
        return None
    # try ISO first
    try:
        return datetime.fromisoformat(s).date()
    except Exception:
        pass
    # try common formats
    for fmt in ('%d/%m/%Y', '%Y-%m-%d'):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            continue
    return None


def add_etudiant(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Invalid method'}, status=405)

    # gather inputs
    nom = request.POST.get('nom') or request.POST.get('prenom') or ''
    prenom = request.POST.get('prenom') or request.POST.get('nom') or ''
    nom_arabe = request.POST.get('nom_arabe') or None
    prenom_arabe = request.POST.get('prenom_arabe') or None
    email = request.POST.get('email')
    telephone = request.POST.get('telephone')
    sexe = request.POST.get('sexe') or None
    date_naissance = _parse_date(request.POST.get('date_naissance') or None)
    # optional date_inscription provided by the add form (string)
    date_inscription_input = request.POST.get('date_inscription') or None
    date_inscription = _parse_date(date_inscription_input) if date_inscription_input else None
    lieu_naissance = request.POST.get('lieu_naissance') or None
    nationalite = request.POST.get('nationalite') or None
    nin = request.POST.get('nin') or None
    adresse = request.POST.get('adresse') or None
    remarques = request.POST.get('remarques') or None
    formation_name = request.POST.get('formation')
    # new fields from the form
    niveau_etude = request.POST.get('niveau_etude') or None
    situation = request.POST.get('situation') or None
    dernier_diplome = request.POST.get('dernier_diplome') or None

    # try to resolve formation by name
    formation = None
    if formation_name:
        formation = Formation.objects.filter(nom__iexact=formation_name).first()

    # handle photo upload if present
    photo_field = None
    if request.FILES.get('photo'):
        f = request.FILES['photo']
        path = default_storage.save(f'etudiants/{f.name}', ContentFile(f.read()))
        photo_field = path

    # if no FK formation found but a text was provided, store it in formation_text
    formation_text = None
    if formation is None and formation_name:
        formation_text = formation_name

    # Normalize NIN and enforce uniqueness at the application level to provide
    # a friendly error before attempting to persist. Database-level unique
    # constraints are recommended as a follow-up migration.
    if nin:
        nin = nin.strip() or None
        if nin and Etudiant.objects.filter(nin__iexact=nin).exists():
            return JsonResponse({'success': False, 'error': 'NIN déjà utilisé par un autre étudiant.'}, status=400)

    try:
        # create using the real DB field name if available
        create_kwargs = dict(
            nom=nom,
            prenom=prenom,
            nom_arabe=nom_arabe,
            prenom_arabe=prenom_arabe,
            email=email,
            telephone=telephone,
            formation=formation,
            formation_text=formation_text,
            photo=photo_field,
            sexe=sexe,
            date_naissance=date_naissance or None,
            lieu_naissance=lieu_naissance,
            nationalite=nationalite,
            nin=nin,
            adresse=adresse,
            remarques=remarques,
            niveau_etude=niveau_etude,
            dernier_diplome=dernier_diplome,
        )
        # assign date_inscription: use provided value if present, otherwise set to today
        try:
            if date_inscription:
                create_kwargs['date_inscription'] = date_inscription
            else:
                create_kwargs['date_inscription'] = timezone.now().date()
        except Exception:
            if date_inscription:
                create_kwargs['date_inscription'] = date_inscription
        # prefer the explicit DB column if present
        if hasattr(Etudiant, 'situation_professionnelle'):
            create_kwargs['situation_professionnelle'] = situation
        else:
            # best-effort fallback: pass a generic attribute via creation kwargs
            create_kwargs['situation'] = situation
        etu = Etudiant.objects.create(**create_kwargs)
        # ensure the value persisted on the instance using helper
        _set_situation(etu, situation)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({'success': False, 'error': str(e)}, status=500)

    student_data = {
        'id': etu.id,
    'date_inscription': etu.date_inscription.isoformat() if getattr(etu, 'date_inscription', None) else '',
        'nom': etu.nom,
    'nom_arabe': etu.nom_arabe if getattr(etu, 'nom_arabe', None) else '',
        'prenom': etu.prenom,
    'prenom_arabe': etu.prenom_arabe if getattr(etu, 'prenom_arabe', None) else '',
        'email': etu.email,
        'telephone': etu.telephone,
        'formation': etu.formation.nom if etu.formation else (etu.formation_text or ''),
        'photo': (settings.MEDIA_URL + etu.photo) if etu.photo else '',
        'statut': etu.statut or 'Actif',
        'sexe': etu.sexe or '',
        'date_naissance': etu.date_naissance.isoformat() if getattr(etu, 'date_naissance', None) else '',
        'lieu_naissance': etu.lieu_naissance or '',
        'nationalite': etu.nationalite or '',
        'nin': etu.nin or '',
        'adresse': etu.adresse or '',
        'remarques': etu.remarques or '',
    'niveau_etude': etu.niveau_etude if getattr(etu, 'niveau_etude', None) else '',
    'situation': _get_situation(etu) or '',
    'dernier_diplome': etu.dernier_diplome if getattr(etu, 'dernier_diplome', None) else '',
    }

    # Helper: send SMS via local modem/router web UI. Best-effort with multiple payload formats.
    def _send_sms_via_modem(phone, text, router_base='http://192.168.1.1/html/sms/message.asp', username='admin', password='admin'):
        try:
            import urllib.request
            import urllib.parse
            import base64
            urls = [router_base, router_base + '?smsType=2']
            payload_variants = [
                {'PhoneNumber': phone, 'Content': text, 'smsType': '2'},
                {'Number': phone, 'Content': text, 'smsType': '2'},
                {'phoneNumber': phone, 'message': text, 'smsType': '2'},
                {'PhoneNumbers': phone, 'Content': text},
                {'Phones': phone, 'Content': text},
                {'to': phone, 'message': text},
            ]
            auth = base64.b64encode(f"{username}:{password}".encode('utf-8')).decode('ascii')
            headers = {
                'Authorization': f'Basic {auth}',
                'User-Agent': 'GeniSchool-SMS-Agent/1.0',
                'Content-Type': 'application/x-www-form-urlencoded',
            }
            for url in urls:
                for payload in payload_variants:
                    try:
                        data = urllib.parse.urlencode(payload).encode('utf-8')
                        req = urllib.request.Request(url, data=data, headers=headers, method='POST')
                        with urllib.request.urlopen(req, timeout=8) as resp:
                            code = getattr(resp, 'status', None) or getattr(resp, 'getcode', lambda: None)()
                            body = resp.read(10240).decode('utf-8', errors='replace')
                            _write_email_log(f"SMS_HTTP_RESP to={phone} url={url} code={code} body_snip={body[:200]!r}")
                            if code and int(code) in (200, 201, 204):
                                # assume success
                                return True
                    except Exception as e:
                        _write_email_log(f"SMS_TRY_FAIL phone={phone} url={url} payload_keys={list(payload.keys())} err={e}")
                        continue
            return False
        except Exception as e_outer:
            _write_email_log(f"SMS_PREP_FAIL phone={phone} err={e_outer}")
            return False

    def _send_sms_via_modem_async(phone, text):
        def _worker():
            ok = _send_sms_via_modem(phone, text)
            if ok:
                _write_email_log(f"SMS_SENT to={phone} ok=1")
            else:
                _write_email_log(f"SMS_SENT to={phone} ok=0")
        t = threading.Thread(target=_worker)
        t.daemon = True
        t.start()

    # send a welcome email to the student if an email address was provided (async)
    email_scheduled = False
    if email:
        print(f"[add_etudiant] scheduling welcome email to: {email}")
        try:
            ctx = {
                'prenom': etu.prenom or '',
                'nom': etu.nom or '',
                'formation': etu.formation.nom if etu.formation else (etu.formation_text or ''),
            }
            # schedule an asynchronous send (force SMTP in the background thread)
            send_welcome_email_async(email, ctx, force_smtp=True)
            # Also start an immediate smtplib send (non-blocking) mirroring the manual test
            try:
                subject = "Bienvenue à GénieSchool"
                template_name = 'email/welcome_etudiant.html'
                html_message = render_to_string(template_name, ctx)
                plain_message = strip_tags(html_message)
                _write_email_log(f"UI_TRIGGER_SMTP_SEND add_etudiant to={email}")
                # Use the direct test-flavor sender per user request, pass rendered
                subject = "Bienvenue à GénieSchool"
                template_name = 'email/welcome_etudiant.html'
                html_message = render_to_string(template_name, ctx)
                plain_message = strip_tags(html_message)
                _smtplib_send_direct_thread(email, subject=subject, html_message=html_message, plain_message=plain_message)
            except Exception as e_smtp:
                _write_email_log(f"UI_TRIGGER_SMTP_FAIL add_etudiant to={email} err={e_smtp}")
            email_scheduled = True
        except Exception as _ex:
            try:
                print('Warning: failed to schedule welcome email to', email, _ex)
            except Exception:
                pass

    # send an SMS via the local modem/router if telephone provided
    sms_scheduled = False
    try:
        tel = (etu.telephone or telephone or '').strip()
        if tel:
            # build a short welcome SMS
            try:
                sms_text = f"Bonjour {etu.prenom} {etu.nom}, bienvenue à GénieSchool. Réf: {etu.id}."
            except Exception:
                sms_text = "Bienvenue à GénieSchool."
            try:
                _send_sms_via_modem_async(tel, sms_text)
                sms_scheduled = True
            except Exception as e_sms:
                _write_email_log(f"SMS_SCHEDULE_FAIL to={tel} err={e_sms}")
    except Exception:
        pass

    resp = {'success': True, 'student': student_data}
    if email_scheduled:
        resp['email_scheduled'] = True
    if sms_scheduled:
        resp['sms_scheduled'] = True
    return JsonResponse(resp)


def reglements_enseignants_view(request):
    # list recent reglements with pagination
    from .models import ReglementEnseignants
    qs = ReglementEnseignants.objects.select_related('enseignant').order_by('-created_at')
    try:
        per_page = int(request.GET.get('per_page') or 20)
    except Exception:
        per_page = 20
    page_number = request.GET.get('page') or 1
    paginator = Paginator(qs, per_page)
    page_obj = paginator.get_page(page_number)
    from .models import Enseignant
    enseignants = Enseignant.objects.all()
    return render(request, 'reglements_enseignants.html', {
        'reglements': page_obj,
        'paginator': paginator,
        'page_obj': page_obj,
        'enseignants': enseignants,
    })


def add_reglement_enseignant(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Invalid method'}, status=405)
    try:
        enseignant_id = int(request.POST.get('enseignant_id'))
    except Exception:
        return JsonResponse({'success': False, 'error': 'Missing enseignant_id'}, status=400)
    from .models import ReglementEnseignants, Enseignant
    enseignant = Enseignant.objects.filter(id=enseignant_id).first()
    if not enseignant:
        return JsonResponse({'success': False, 'error': 'Enseignant introuvable'}, status=404)
    try:
        montant = Decimal(request.POST.get('montant') or '0')
    except Exception:
        return JsonResponse({'success': False, 'error': 'Montant invalide'}, status=400)
    mode = request.POST.get('mode') or ''
    statut = request.POST.get('statut') or ''
    reference = request.POST.get('reference') or None
    remarques = request.POST.get('remarques') or ''
    date_reglement = _parse_date(request.POST.get('date_reglement')) or date.today()
    # create
    obj = ReglementEnseignants.objects.create(
        enseignant=enseignant,
        montant=montant,
        balance_after=None,
        date_reglement=date_reglement,
        mode_reglement=mode,
        statut=statut,
        reference=reference,
        remarques=remarques,
    )
    data = {
        'id': obj.id,
        'enseignant': str(obj.enseignant),
        'montant': str(obj.montant),
        'date_reglement': obj.date_reglement.isoformat() if obj.date_reglement else '',
        'mode_reglement': obj.mode_reglement or '',
        'statut': obj.statut or '',
        'reference': obj.reference or '',
    }
    return JsonResponse({'success': True, 'reglement': data})


def edit_reglement_enseignant(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Invalid method'}, status=405)
    try:
        rid = int(request.POST.get('id') or request.POST.get('_id'))
    except Exception:
        return JsonResponse({'success': False, 'error': 'Missing id'}, status=400)
    from .models import ReglementEnseignants, Enseignant
    obj = ReglementEnseignants.objects.filter(id=rid).first()
    if not obj:
        return JsonResponse({'success': False, 'error': 'Not found'}, status=404)
    try:
        if request.POST.get('enseignant_id'):
            ens = Enseignant.objects.filter(id=int(request.POST.get('enseignant_id'))).first()
            if ens: obj.enseignant = ens
        if request.POST.get('montant') is not None:
            obj.montant = Decimal(request.POST.get('montant') or obj.montant)
        if request.POST.get('date_reglement'):
            parsed = _parse_date(request.POST.get('date_reglement'))
            if parsed: obj.date_reglement = parsed
        if request.POST.get('mode') is not None:
            obj.mode_reglement = request.POST.get('mode')
        if request.POST.get('statut') is not None:
            obj.statut = request.POST.get('statut')
    # Reference must not be changed via edit form — ignore any submitted reference
        if request.POST.get('remarques') is not None:
            obj.remarques = request.POST.get('remarques')
        obj.save()
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)
    return JsonResponse({'success': True, 'reglement': {
        'id': obj.id,
        'enseignant': str(obj.enseignant),
        'montant': str(obj.montant),
        'date_reglement': obj.date_reglement.isoformat() if obj.date_reglement else '',
        'mode_reglement': obj.mode_reglement or '',
        'statut': obj.statut or '',
        'reference': obj.reference or ''
    }})


def delete_reglement_enseignant(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Invalid method'}, status=405)
    try:
        rid = int(request.POST.get('id') or request.POST.get('_id'))
    except Exception:
        return JsonResponse({'success': False, 'error': 'Missing id'}, status=400)
    from .models import ReglementEnseignants
    obj = ReglementEnseignants.objects.filter(id=rid).first()
    if not obj:
        return JsonResponse({'success': False, 'error': 'Not found'}, status=404)
    obj.delete()
    return JsonResponse({'success': True})


def edit_etudiant(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Invalid method'}, status=405)
    try:
        etu_id = int(request.POST.get('id') or request.POST.get('_id'))
    except Exception:
        return JsonResponse({'success': False, 'error': 'Missing id'}, status=400)
    etu = Etudiant.objects.filter(id=etu_id).first()
    if not etu:
        return JsonResponse({'success': False, 'error': 'Not found'}, status=404)
    etu.nom = request.POST.get('nom') or etu.nom
    etu.prenom = request.POST.get('prenom') or etu.prenom
    old_email = etu.email
    new_email = request.POST.get('email') or etu.email
    etu.email = new_email
    etu.telephone = request.POST.get('telephone') or etu.telephone
    # edit new fields
    etu.nom_arabe = request.POST.get('nom_arabe') or etu.nom_arabe
    etu.prenom_arabe = request.POST.get('prenom_arabe') or etu.prenom_arabe
    etu.sexe = request.POST.get('sexe') or etu.sexe
    # update the new fields if present
    if request.POST.get('niveau_etude') is not None:
        etu.niveau_etude = request.POST.get('niveau_etude') or None
    if request.POST.get('situation') is not None:
        # use flexible setter to handle legacy DB column name
        _set_situation(etu, request.POST.get('situation') or None)
    if request.POST.get('dernier_diplome') is not None:
        etu.dernier_diplome = request.POST.get('dernier_diplome') or None
    dn = request.POST.get('date_naissance')
    if dn:
        parsed = _parse_date(dn)
        if parsed:
            etu.date_naissance = parsed
    etu.lieu_naissance = request.POST.get('lieu_naissance') or etu.lieu_naissance
    etu.nationalite = request.POST.get('nationalite') or etu.nationalite
    # enforce NIN uniqueness on edit: if a different student already has this NIN, reject
    new_nin = (request.POST.get('nin') or '').strip() or None
    if new_nin and Etudiant.objects.filter(nin__iexact=new_nin).exclude(id=etu.id).exists():
        return JsonResponse({'success': False, 'error': 'NIN déjà utilisé par un autre étudiant.'}, status=400)
    if new_nin is not None:
        etu.nin = new_nin
    etu.adresse = request.POST.get('adresse') or etu.adresse
    etu.remarques = request.POST.get('remarques') or etu.remarques
    # Envoi email si l'email a changé et qu'il est valide
    email_scheduled = False
    if old_email != new_email and new_email:
        context = {
            'prenom': etu.prenom,
            'nom': etu.nom,
            'formation': etu.formation.nom if etu.formation else (etu.formation_text or ''),
        }
        print(f"[email] edit_etudiant: email changed from '{old_email}' to '{new_email}', scheduling welcome email")
        # schedule background send (force SMTP inside thread)
        try:
            send_welcome_email_async(new_email, context, force_smtp=True)
            # Also trigger an immediate smtplib send in background (non-blocking)
            try:
                subject = "Bienvenue à GénieSchool"
                template_name = 'email/welcome_etudiant.html'
                html_message = render_to_string(template_name, context)
                plain_message = strip_tags(html_message)
                _write_email_log(f"UI_TRIGGER_SMTP_SEND edit_etudiant to={new_email}")
                # Use the direct test-flavor sender per user request, pass rendered
                subject = "Bienvenue à GénieSchool"
                template_name = 'email/welcome_etudiant.html'
                html_message = render_to_string(template_name, context)
                plain_message = strip_tags(html_message)
                _smtplib_send_direct_thread(new_email, subject=subject, html_message=html_message, plain_message=plain_message)
            except Exception as e_smtp:
                _write_email_log(f"UI_TRIGGER_SMTP_FAIL edit_etudiant to={new_email} err={e_smtp}")
            email_scheduled = True
        except Exception as e_sched:
            print('[email] edit_etudiant: failed to schedule welcome email:', e_sched)
    formation_name = request.POST.get('formation')
    if formation_name:
        formation = Formation.objects.filter(nom__iexact=formation_name).first()
        etu.formation = formation
        if not formation:
            etu.formation_text = formation_name
        else:
            etu.formation_text = None
    # handle photo file
    if request.FILES.get('photo'):
        f = request.FILES['photo']
        path = default_storage.save(f'etudiants/{f.name}', ContentFile(f.read()))
        etu.photo = path
    etu.save()
    resp = {'success': True, 'student': {
        'id': etu.id,
        'nom': etu.nom,
    'nom_arabe': etu.nom_arabe if getattr(etu, 'nom_arabe', None) else '',
        'prenom': etu.prenom,
    'prenom_arabe': etu.prenom_arabe if getattr(etu, 'prenom_arabe', None) else '',
        'email': etu.email,
        'telephone': etu.telephone,
    'formation': etu.formation.nom if etu.formation else (etu.formation_text or ''),
    'photo': (settings.MEDIA_URL + etu.photo) if etu.photo else '',
    'statut': etu.statut or 'Actif',
    'sexe': etu.sexe or '',
    'date_naissance': etu.date_naissance.isoformat() if getattr(etu, 'date_naissance', None) else '',
    'lieu_naissance': etu.lieu_naissance or '',
    'nationalite': etu.nationalite or '',
    'nin': etu.nin or '',
    'adresse': etu.adresse or '',
    'remarques': etu.remarques or '',
    'niveau_etude': etu.niveau_etude if getattr(etu, 'niveau_etude', None) else '',
    'situation': _get_situation(etu) or '',
    'dernier_diplome': etu.dernier_diplome if getattr(etu, 'dernier_diplome', None) else '',
    }}
    if email_scheduled:
        resp['email_scheduled'] = True
    return JsonResponse(resp)


def delete_etudiant(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Invalid method'}, status=405)
    try:
        etu_id = int(request.POST.get('id') or request.POST.get('_id'))
    except Exception:
        return JsonResponse({'success': False, 'error': 'Missing id'}, status=400)
    etu = Etudiant.objects.filter(id=etu_id).first()
    if not etu:
        return JsonResponse({'success': False, 'error': 'Not found'}, status=404)
    etu.delete()
    return JsonResponse({'success': True})


def etudiant_detail_json(request, pk):
    """Return a JSON object with student details and recent paiements."""
    try:
        etu = Etudiant.objects.filter(id=pk).first()
        if not etu:
            return JsonResponse({'success': False, 'error': 'Not found'}, status=404)
        # student basic data
        photo = ''
        try:
            if getattr(etu, 'photo', None):
                # if FileField, use url when available
                photo = getattr(etu.photo, 'url', '') or str(settings.MEDIA_URL + str(etu.photo))
        except Exception:
            photo = ''
        # student core dict
        student = {
            'id': etu.id,
            'nom': etu.nom,
            'prenom': etu.prenom,
            'email': etu.email or '',
            'telephone': etu.telephone or '',
            'formation': etu.formation.nom if getattr(etu, 'formation', None) else (getattr(etu, 'formation_text', '') or ''),
            'photo': photo,
            'photo_url': photo,
            'date_inscription': etu.date_inscription.isoformat() if getattr(etu, 'date_inscription', None) else '',
            'inscription_status': getattr(etu, 'inscription_status', None) or getattr(etu, 'status', '') or '',
            'date_naissance': etu.date_naissance.isoformat() if getattr(etu, 'date_naissance', None) else '',
            'adresse': etu.adresse or '',
            'nin': etu.nin or '',
            'nationalite': etu.nationalite or '',
            'niveau_etude': etu.niveau_etude or '',
            'dernier_diplome': etu.dernier_diplome or '',
            'remarques': etu.remarques or '',
        }
        # collect recent paiements (limit 50)
        paiements = []
        try:
            qs = Paiement.objects.filter(etudiant_id=etu.id).order_by('-date_paiement')[:50]
            for p in qs:
                paiements.append({
                    'id': p.id,
                    'montant': str(p.montant),
                    'date_paiement': p.date_paiement.isoformat() if getattr(p, 'date_paiement', None) else '',
                    'mode': getattr(p, 'mode_paiement', None) or getattr(p, 'mode_reglement', None) or getattr(p, 'mode', None) or '',
                    'reference': getattr(p, 'reference', '') or '',
                    'remarques': getattr(p, 'remarques', '') or '',
                })
        except Exception:
            paiements = []

        # inscriptions
        inscriptions = []
        try:
            ins_qs = Inscription.objects.filter(etudiant_id=etu.id).select_related('formation').order_by('-date_inscription')[:30]
            for ins in ins_qs:
                inscriptions.append({
                    'id': ins.id,
                    'formation': ins.formation.nom if getattr(ins, 'formation', None) else (ins.formation_text if getattr(ins, 'formation_text', None) else ''),
                    'date_inscription': ins.date_inscription.isoformat() if getattr(ins, 'date_inscription', None) else '',
                    'prix_total': str(ins.prix_total) if getattr(ins, 'prix_total', None) else '',
                    'session': ins.session or '',
                    'groupe': (ins.groupe.nom if getattr(ins, 'groupe', None) and getattr(ins.groupe, 'nom', None) else ''),
                    'statut': ins.statut or '',
                    'remarques': ins.remarques or '',
                })
        except Exception:
            inscriptions = []

        # invoices related to this student (either via inscription or paiement)
        invoices = []
        try:
            inv_qs = Invoice.objects.filter(Q(inscription__etudiant=etu) | Q(paiement__etudiant=etu)).order_by('-date_emission')[:50]
            for inv in inv_qs:
                invoices.append({
                    'id': inv.id,
                    'numero': inv.numero or '',
                    'montant': str(inv.montant),
                    'date_emission': inv.date_emission.isoformat() if getattr(inv, 'date_emission', None) else '',
                    'date_echeance': inv.date_echeance.isoformat() if getattr(inv, 'date_echeance', None) else '',
                    'statut': inv.statut or '',
                    'remarques': inv.remarques or '',
                })
        except Exception:
            invoices = []

        # totals: sum of inscriptions.prix_total and paiements total
        try:
            total_due = float(Inscription.objects.filter(etudiant_id=etu.id).aggregate(total=Sum('prix_total'))['total'] or 0)
        except Exception:
            total_due = 0.0
        try:
            total_paid = float(Paiement.objects.filter(etudiant_id=etu.id).aggregate(total=Sum('montant'))['total'] or 0)
        except Exception:
            total_paid = 0.0
        balance = total_due - total_paid

        # documents: build urls for any document paths stored as strings
        documents = {}
        try:
            if etu.extrait_naissance_photo:
                documents['extrait_naissance'] = str(settings.MEDIA_URL + str(etu.extrait_naissance_photo))
            if etu.carte_identite_photo:
                documents['carte_identite'] = str(settings.MEDIA_URL + str(etu.carte_identite_photo))
        except Exception:
            documents = {}

        return JsonResponse({'success': True, 'student': student, 'paiements': paiements, 'inscriptions': inscriptions, 'invoices': invoices, 'totals': {'due': total_due, 'paid': total_paid, 'balance': balance}, 'documents': documents})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


def formations_view(request):
    # Try to annotate formations with a boolean 'in_progress' when at least one inscription exists
    try:
        from django.db.models import Exists, OuterRef
        ins_qs = Inscription.objects.filter(formation=OuterRef('pk'))
        qs = Formation.objects.annotate(in_progress=Exists(ins_qs)).order_by('-id')
    except Exception:
        qs = Formation.objects.all().order_by('-id')

    # compute photo_url for each formation like in etudiants_view so templates can use f.photo_url
    for f in qs:
        try:
            f.photo_url = ''
            photo_field = getattr(f, 'photo', None)
            if photo_field:
                try:
                    # prefer storage.url if available
                    f.photo_url = default_storage.url(photo_field)
                except Exception:
                    # fallback to MEDIA_URL + stored value
                    try:
                        f.photo_url = (settings.MEDIA_URL + str(photo_field)) if photo_field else ''
                    except Exception:
                        f.photo_url = str(photo_field) if photo_field else ''
            # ensure boolean attribute in_progress always exists (fallback when annotation failed)
            if not hasattr(f, 'in_progress'):
                try:
                    f.in_progress = Inscription.objects.filter(formation_id=getattr(f, 'id', None)).exists()
                except Exception:
                    f.in_progress = False
        except Exception:
            f.photo_url = ''
            try:
                if f.photo_url:
                    f.photo_url = f.photo_url.replace('\\', '/')
                    if not f.photo_url.startswith('http') and not f.photo_url.startswith('/'):
                        f.photo_url = (settings.MEDIA_URL or '/') + f.photo_url
            except Exception:
                pass

    return render(request, 'formations.html', {'formations': qs, 'MEDIA_URL': settings.MEDIA_URL})


@xframe_options_exempt
def formation_pdf(request, pk):
    """Return a fillable PDF (AcroForm) for a given formation.
    The PDF will include editable fields for name, contenu, duree and prix_etudiant.
    """
    try:
        f = Formation.objects.filter(id=pk).first()
        if not f:
            return HttpResponse('Not found', status=404)

        # lazy import reportlab to avoid hard dependency until used
        try:
            from reportlab.pdfgen import canvas
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.units import mm
        except Exception:
            # If reportlab is not available, return a simple HTML preview as a graceful fallback.
            # This avoids requiring extra packages while still providing a usable preview in the iframe.
            try:
                html = f"""<!doctype html>
<html><head><meta charset='utf-8'><title>{(f.nom or 'Formation')}</title>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<style>body{{font-family:Arial,Helvetica,sans-serif;padding:18px;background:#fff;color:#111}} .header{{margin-bottom:12px}} .meta{{color:#555;margin-bottom:8px}} .content{{white-space:pre-wrap}}</style>
</head><body>
<div class='header'><h1>{(f.nom or 'Formation')}</h1></div>
<div class='meta'>Durée: { (f.duree or '-') } &nbsp; | &nbsp; Prix: { (getattr(f,'prix_etudiant','') or '-') }</div>
<div class='content'>{(f.contenu or 'Aucune description fournie.')}</div>
</body></html>"""
            except Exception:
                html = '<html><body><h1>Formation</h1><p>Contenu indisponible.</p></body></html>'
            return HttpResponse(html, content_type='text/html')

        buffer = BytesIO()
        c = canvas.Canvas(buffer, pagesize=A4)
        width, height = A4

        left = 20 * mm
        top = height - 20 * mm

        # Title
        c.setFont('Helvetica-Bold', 16)
        c.drawString(left, top, f.nom or 'Formation')

        # Small labels and form fields (using acroform)
        form = c.acroForm

        y = top - 14 * mm
        c.setFont('Helvetica', 10)
        c.drawString(left, y + 10, 'Durée:')
        form.textfield(name='duree', value=str(f.duree or ''), x=left + 30 * mm, y=y, width=70 * mm, height=8 * mm, borderStyle='underlined', forceBorder=True)

        y -= 12 * mm
        c.drawString(left, y + 10, 'Prix étudiant:')
        form.textfield(name='prix_etudiant', value=str(getattr(f, 'prix_etudiant', '') or ''), x=left + 30 * mm, y=y, width=70 * mm, height=8 * mm, borderStyle='underlined', forceBorder=True)

        y -= 16 * mm
        c.drawString(left, y + 10, 'Branche:')
        form.textfield(name='branche', value=str(getattr(f, 'branche', '') or ''), x=left + 30 * mm, y=y, width=70 * mm, height=8 * mm, borderStyle='underlined', forceBorder=True)

        # Large multi-line field for contenu
        y -= 20 * mm
        c.drawString(left, y + (40 * mm) + 10, 'Contenu (modifiable):')
        form.textfield(name='contenu', value=str(f.contenu or ''), x=left, y=y, width=width - 40 * mm, height=40 * mm, borderStyle='solid', multiline=True, forceBorder=True)

        # If a PDF file was previously uploaded for this formation, serve it directly
        try:
            if getattr(f, 'pdf', None):
                try:
                    pdf_path = f.pdf
                    with default_storage.open(pdf_path, 'rb') as fh:
                        data = fh.read()
                    resp = HttpResponse(data, content_type='application/pdf')
                    resp['Content-Disposition'] = f'inline; filename=formation_{pk}.pdf'
                    return resp
                except Exception:
                    # fallback to upload page if stored file cannot be read
                    pass
        except Exception:
            pass

        # No uploaded PDF available: return an HTML page that allows uploading a full PDF.
        # The page posts the PDF to the existing edit endpoint and then reloads the iframe
        edit_url = reverse('formations_edit')
        pdf_url = reverse('formation_pdf', args=[pk])

        # Build a simple HTML upload page without f-string brace conflicts
        html = (
            '<!doctype html><html><head><meta charset="utf-8"><title>Upload PDF - ' +
            (f.nom or 'Formation') +
            '</title><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<style>body{font-family:Arial,Helvetica,sans-serif;padding:18px} '
            '.box{max-width:720px;margin:18px auto;padding:18px;border:1px solid #e6e6e6;border-radius:8px} '
            '.btn{padding:8px 12px;border-radius:8px;background:#10b981;color:#fff;border:none;cursor:pointer} '
            '</style></head><body>'
            '<div class="box">'
            '<h2>Pas de PDF disponible pour: ' + (f.nom or 'Formation') + '</h2>'
            '<p>Vous pouvez téléverser un PDF complet pour cette formation. Le fichier sera enregistré et affiché ensuite.</p>'
            '<input type="file" id="pdfFile" accept="application/pdf" />'
            '<div style="margin-top:12px;display:flex;gap:8px"><button id="uploadBtn" class="btn">Téléverser</button>'
            '<button id="cancelBtn">Annuler</button></div>'
            '<div id="status" style="margin-top:10px;color:#666"></div>'
            '</div>'
            '<script>'
            'function getCookie(name){var cookieValue=null; if(document.cookie && document.cookie!==""){var cookies=document.cookie.split(";"); for(var i=0;i<cookies.length;i++){var c=cookies[i].trim(); if(c.substring(0,name.length+1)===(name+"=")){cookieValue=decodeURIComponent(c.substring(name.length+1)); break;}}} return cookieValue; } '
            'var csrftoken = getCookie("csrftoken"); '
            'document.getElementById("uploadBtn").addEventListener("click", function(){ '
            'var ffile = document.getElementById("pdfFile").files[0]; if(!ffile){ document.getElementById("status").textContent="Veuillez choisir un fichier PDF."; return; } '
            'var fd = new FormData(); fd.append("id","' + str(pk) + '"); fd.append("pdf", ffile); '
            'document.getElementById("status").textContent="Téléversement en cours..."; '
            'fetch("' + edit_url + '", { method: "POST", headers: {"X-CSRFToken": csrftoken}, body: fd })'
            '.then(function(r){ return r.text().then(function(text){ if(!r.ok){ try{ var j = JSON.parse(text); throw new Error(j.error||text||"Erreur serveur"); }catch(e){ throw new Error(text||"Erreur serveur"); } } try{ return JSON.parse(text); }catch(e){ throw new Error("Invalid JSON response"); } }); })'
            '.then(function(json){ if(json.success){ document.getElementById("status").textContent="Téléversement réussi. Chargement du PDF..."; window.location.href = "' + pdf_url + '"; } else { document.getElementById("status").textContent = "Erreur: " + (json.error||"Échec"); } })'
            '.catch(function(err){ document.getElementById("status").textContent = "Erreur: "+(err.message||"Erreur réseau"); }); }); '
            'document.getElementById("cancelBtn").addEventListener("click", function(){ window.history.back(); });'
            '</script></body></html>'
        )
        return HttpResponse(html, content_type='text/html')
    except Exception as e:
        import traceback
        traceback.print_exc()
        return HttpResponse('Server error: %s' % str(e), status=500)


def add_formation(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Invalid method'}, status=405)
    try:
        # parse inputs and create formation
        nom = (request.POST.get('nom') or '').strip()
        # prevent duplicate formation by name (case-insensitive)
        if nom:
            exists = Formation.objects.filter(nom__iexact=nom).exists()
            if exists:
                return JsonResponse({'success': False, 'error': 'Une formation avec ce nom existe déjà.'}, status=400)
        contenu = request.POST.get('contenu') or ''
        prix_etudiant = request.POST.get('prix_etudiant') or None
        prix_fonctionnaire = request.POST.get('prix_fonctionnaire') or None
        duree = request.POST.get('duree') or ''
        branche = request.POST.get('branche') or ''
        categorie = request.POST.get('categorie') or ''
        niveau = request.POST.get('niveau') or ''
        statut = request.POST.get('statut') or 'active'
        photo_field = None
        if request.FILES.get('photo'):
            f = request.FILES['photo']
            path = default_storage.save(f'formations/{f.name}', ContentFile(f.read()))
            photo_field = path

        f = Formation.objects.create(
            nom=nom,
            contenu=contenu,
            prix_etudiant=prix_etudiant or None,
            prix_fonctionnaire=prix_fonctionnaire or None,
            duree=duree,
            branche=branche,
            categorie=categorie,
            niveau=niveau,
            statut=statut,
            photo=photo_field,
        )

        # compute returned photo url safely
        photo_url = ''
        try:
            if getattr(f, 'photo', None):
                try:
                    photo_url = default_storage.url(f.photo)
                except Exception:
                    try:
                        photo_url = f.photo.url if getattr(f.photo, 'url', None) else (settings.MEDIA_URL + str(f.photo))
                    except Exception:
                        photo_url = str(f.photo)
        except Exception:
            photo_url = getattr(f, 'photo', '') or ''

        def _fmt_price(val):
            if val is None:
                return None
            s = str(val)
            if s.endswith('.00'):
                return s[:-3]
            return s

        data = {
            'id': f.id,
            'nom': f.nom,
            'contenu': f.contenu or '',
            'photo': photo_url,
            'prix_etudiant': _fmt_price(f.prix_etudiant),
            'prix_fonctionnaire': _fmt_price(f.prix_fonctionnaire),
            'duree': f.duree or '',
            'branche': f.branche or '',
            'categorie': f.categorie or '',
            'niveau': f.niveau or '',
            'statut': f.statut or '',
        }

        return JsonResponse({'success': True, 'formation': data})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


def edit_formation(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Invalid method'}, status=405)
    try:
        try:
            fid = int(request.POST.get('id') or request.POST.get('_id'))
        except Exception:
            return JsonResponse({'success': False, 'error': 'Missing id'}, status=400)

        obj = Formation.objects.filter(id=fid).first()
        if not obj:
            return JsonResponse({'success': False, 'error': 'Not found'}, status=404)

        obj.nom = request.POST.get('nom') or obj.nom
        # use 'contenu' for the long content field
        obj.contenu = request.POST.get('contenu') or obj.contenu
        obj.prix_etudiant = request.POST.get('prix_etudiant') or obj.prix_etudiant
        obj.prix_fonctionnaire = request.POST.get('prix_fonctionnaire') or obj.prix_fonctionnaire
        obj.duree = request.POST.get('duree') or obj.duree
        obj.branche = request.POST.get('branche') or obj.branche
        obj.categorie = request.POST.get('categorie') or obj.categorie
        obj.niveau = request.POST.get('niveau') or obj.niveau
        obj.statut = request.POST.get('statut') or obj.statut

        if request.FILES.get('photo'):
            f = request.FILES['photo']
            path = default_storage.save(f'formations/{f.name}', ContentFile(f.read()))
            obj.photo = path
        # accept uploaded PDF file (field name 'pdf') and store it under formations/pdfs/
        if request.FILES.get('pdf'):
            pf = request.FILES['pdf']
            try:
                pdf_path = default_storage.save(f'formations/pdfs/{pf.name}', ContentFile(pf.read()))
                obj.pdf = pdf_path
            except Exception:
                # if saving fails, ignore and continue (error surfaced later if needed)
                pass

        obj.save()
        def _fmt_price(val):
            if val is None:
                return None
            s = str(val)
            if s.endswith('.00'):
                return s[:-3]
            return s

        data = {
            'id': obj.id,
            'nom': obj.nom,
            'contenu': obj.contenu or '',
            'photo': (settings.MEDIA_URL + obj.photo) if getattr(obj, 'photo', None) else '',
            'pdf': (settings.MEDIA_URL + obj.pdf) if getattr(obj, 'pdf', None) else '',
            'prix_etudiant': _fmt_price(obj.prix_etudiant),
            'prix_fonctionnaire': _fmt_price(obj.prix_fonctionnaire),
            'duree': obj.duree or '',
            'branche': obj.branche or '',
            'categorie': obj.categorie or '',
            'niveau': obj.niveau or '',
            'statut': obj.statut or '',
        }
        return JsonResponse({'success': True, 'formation': data})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


def delete_formation(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Invalid method'}, status=405)
    try:
        fid = int(request.POST.get('id') or request.POST.get('_id'))
    except Exception:
        return JsonResponse({'success': False, 'error': 'Missing id'}, status=400)
    obj = Formation.objects.filter(id=fid).first()
    if not obj:
        return JsonResponse({'success': False, 'error': 'Not found'}, status=404)
    obj.delete()
    return JsonResponse({'success': True})


def salle_list(request):
    """Return list of salles as JSON for client-side refresh."""
    try:
        qs = Salle.objects.all().order_by('id')
        data = []
        for s in qs:
            data.append({'id': s.id, 'nom': s.nom, 'capacite': s.capacite, 'equipements': s.equipements or ''})
        return JsonResponse({'success': True, 'salles': data})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


def salle_upsert(request):
    """Create or update a Salle via POST (id optional). Returns salle JSON."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Invalid method'}, status=405)
    try:
        sid = request.POST.get('id') or None
        nom = (request.POST.get('nom') or '').strip()
        capacite_raw = request.POST.get('capacite') or None
        equipements = request.POST.get('equipements') or ''

        # Basic validation: name required
        if not nom:
            return JsonResponse({'success': False, 'error': 'Missing nom'}, status=400)

        # Try to coerce capacity to int when provided
        capacite = None
        if capacite_raw not in (None, ''):
            try:
                capacite = int(capacite_raw)
            except Exception:
                return JsonResponse({'success': False, 'error': 'Capacité doit être un entier'}, status=400)

        # If creating, ensure unique nom (server-side check)
        if not sid:
            if Salle.objects.filter(nom__iexact=nom).exists():
                return JsonResponse({'success': False, 'error': 'Nom de salle déjà utilisé'}, status=400)
            s = Salle()
            s.nom = nom
            s.capacite = capacite
            s.equipements = equipements
            s.save()
            return JsonResponse({'success': True, 'salle': {'id': s.id, 'nom': s.nom, 'capacite': s.capacite, 'equipements': s.equipements or ''}})

        # update
        s = Salle.objects.filter(id=sid).first()
        if not s:
            return JsonResponse({'success': False, 'error': 'Salle introuvable'}, status=404)
        # check unique name on update
        if s.nom.lower() != nom.lower() and Salle.objects.filter(nom__iexact=nom).exclude(id=s.id).exists():
            return JsonResponse({'success': False, 'error': 'Nom de salle déjà utilisé par une autre'}, status=400)
        s.nom = nom
        s.capacite = capacite
        s.equipements = equipements
        s.save()
        return JsonResponse({'success': True, 'salle': {'id': s.id, 'nom': s.nom, 'capacite': s.capacite, 'equipements': s.equipements or ''}})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


def groupe_add(request):
    """Create a Groupe for a given formation. POST fields: formation (id), nom (string).
    Returns JSON with created groupe."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Invalid method'}, status=405)
    try:
        fid = int(request.POST.get('formation') or 0)
    except Exception:
        return JsonResponse({'success': False, 'error': 'Invalid formation id'}, status=400)
    nom = (request.POST.get('nom') or '').strip()
    if not nom:
        return JsonResponse({'success': False, 'error': 'Missing nom'}, status=400)
    formation = Formation.objects.filter(id=fid).first()
    if not formation:
        return JsonResponse({'success': False, 'error': 'Formation not found'}, status=404)
    # ensure unique per formation
    if Groupe.objects.filter(nom__iexact=nom, formation=formation).exists():
        return JsonResponse({'success': False, 'error': 'Groupe déjà existant pour cette formation'}, status=400)
    # optional capacity
    capacite = None
    try:
        cap_raw = request.POST.get('capacite')
        if cap_raw is not None and str(cap_raw).strip() != '':
            capacite = int(cap_raw)
    except Exception:
        capacite = None
    g = Groupe(nom=nom, formation=formation, capacite=capacite)
    g.save()
    return JsonResponse({'success': True, 'groupe': {'id': g.id, 'nom': g.nom, 'formation_id': formation.id}})


def groupe_delete(request):
    """Delete a Groupe by id (POST). Safety: prevent deletion if inscriptions exist for the groupe."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Invalid method'}, status=405)
    # Accept either id OR formation+nom pair
    g = None
    try:
        gid = request.POST.get('id')
        if gid:
            g = Groupe.objects.filter(id=int(gid)).first()
    except Exception:
        g = None
    if not g:
        # try formation+nom
        try:
            fid = int(request.POST.get('formation') or 0)
            nom = (request.POST.get('nom') or '').strip()
            if fid and nom:
                formation = Formation.objects.filter(id=fid).first()
                if formation:
                    g = Groupe.objects.filter(formation=formation, nom__iexact=nom).first()
        except Exception:
            g = None
    if not g:
        return JsonResponse({'success': False, 'error': 'Not found'}, status=404)
    # if inscriptions reference this groupe, prevent deletion
    if Inscription.objects.filter(groupe=g).exists():
        return JsonResponse({'success': False, 'error': 'Impossible de supprimer: des inscriptions existent pour ce groupe'}, status=400)
    g.delete()
    return JsonResponse({'success': True})
    


def salle_delete(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Invalid method'}, status=405)
    try:
        sid = request.POST.get('id') or request.POST.get('_id')
        if not sid:
            return JsonResponse({'success': False, 'error': 'Missing id'}, status=400)
        s = Salle.objects.filter(id=sid).first()
        if not s:
            return JsonResponse({'success': False, 'error': 'Not found'}, status=404)
        s.delete()
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


def formateurs_view(request):
    count = Utilisateur.objects.filter(statut__icontains='formateur').count() or AvailabilityFormateur.objects.values('utilisateur').distinct().count()
    return render(request, 'formateurs.html', {'count': count})


def planning_view(request):
    sessions = Session.objects.count()
    # Provide formations, formateurs and salles so the template can populate selects
    formations = Formation.objects.all()
    try:
        formateurs_qs = Enseignant.objects.all()
        if not formateurs_qs.exists():
            formateurs_qs = Utilisateur.objects.filter(statut__icontains='formateur')
    except Exception:
        formateurs_qs = Utilisateur.objects.filter(statut__icontains='formateur')

    try:
        salles_qs = Salle.objects.all()
    except Exception:
        salles_qs = []

    # build per-formation groupe options from actual Groupe rows (match ins/disabled using capacite)
    import json
    formation_groups = {}
    DEFAULT_GROUP_CAPACITY = 16
    from .models import Groupe as GroupeModel
    for f in formations:
        opts = []
        try:
            groupes_qs = GroupeModel.objects.filter(formation=f).order_by('created_at')
        except Exception:
            groupes_qs = []
        for g in groupes_qs:
            try:
                cap = int(g.capacite) if g.capacite is not None else DEFAULT_GROUP_CAPACITY
            except Exception:
                cap = DEFAULT_GROUP_CAPACITY
            try:
                cnt = Inscription.objects.filter(formation=f, groupe=g).count()
            except Exception:
                cnt = 0
            opts.append({'id': g.id, 'nom': g.nom, 'disabled': cnt >= cap, 'count': cnt, 'capacity': cap})
        formation_groups[str(f.id)] = opts

    return render(request, 'planning.html', {
        'sessions': sessions,
        'formations': formations,
        'formateurs': formateurs_qs,
        'salles': salles_qs,
        'formation_groups_json': json.dumps(formation_groups),
    })


def planning_events_api(request):
    """Return calendar events and sessions for a given week.
    Query param: week_start=YYYY-MM-DD (Sunday). If omitted, uses current week start.
    """
    week_start_str = request.GET.get('week_start')
    try:
        if week_start_str:
            ws_date = datetime.fromisoformat(week_start_str).date()
        else:
            today = timezone.localdate()
            ws_date = today - timezone.timedelta(days=today.weekday()+1 if today.weekday()<6 else 0)
    except Exception:
        ws_date = timezone.localdate()

    # If caller provided a week_start, treat it as the week's start date.
    # Otherwise compute the start of the current week (previous Sunday).
    if week_start_str:
        sunday = ws_date
    else:
        try:
            today = timezone.localdate()
            # compute previous Sunday
            delta = (today.weekday() + 1) % 7
            sunday = today - timezone.timedelta(days=delta)
        except Exception:
            sunday = ws_date

    # Use date-range filtering to avoid timezone mismatch: compare start_datetime__date
    evs = []
    try:
        end_date = sunday + timezone.timedelta(days=7)
        q_events = CalendarEvent.objects.filter(start_datetime__date__gte=sunday, start_datetime__date__lt=end_date)
        for e in q_events:
            # CalendarEvent stores integer id fields (salle_id, formation_id, organisateur_id)
            # and optionally stores textual name fields (formation_name, formateur_name, salle_name).
            # If the textual fields are empty but the id fields are present, try to resolve
            # the human-readable names from the related models to improve client display.
            salle_id = getattr(e, 'salle_id', None)
            formation_id = getattr(e, 'formation_id', None)
            organisateur_id = getattr(e, 'organisateur_id', None)

            salle_name = (getattr(e, 'salle_name', '') or '')
            formation_name = (getattr(e, 'formation_name', '') or '')
            formateur_name = (getattr(e, 'formateur_name', '') or '')

            # resolve salle name if missing
            if not salle_name and salle_id:
                try:
                    s_obj = Salle.objects.filter(id=salle_id).values_list('nom', flat=True).first()
                    if s_obj:
                        salle_name = s_obj
                except Exception:
                    pass

            # resolve formation name if missing
            if not formation_name and formation_id:
                try:
                    f_obj = Formation.objects.filter(id=formation_id).values_list('nom', flat=True).first()
                    if f_obj:
                        formation_name = f_obj
                except Exception:
                    pass

            # resolve organisateur/formateur name if missing
            if not formateur_name and organisateur_id:
                try:
                    fo = Enseignant.objects.filter(id=organisateur_id).first()
                    if fo:
                        formateur_name = str(fo)
                    else:
                        u = Utilisateur.objects.filter(id=organisateur_id).first()
                        if u:
                            formateur_name = str(u)
                except Exception:
                    pass

            # Ensure we always provide a readable label: prefer formation_name, fall back to event title
            display_formation = formation_name or e.titre or ''
            evs.append({
                'id': e.id,
                'title': e.titre,
                'start': e.start_datetime.isoformat(),
                'end': e.end_datetime.isoformat() if e.end_datetime else None,
                'type': 'calendar_event',
                'salle': salle_id,
                'salle_name': salle_name,
                'formation': formation_id,
                'formation_name': formation_name,
                # include groupe (string) when present on CalendarEvent
                'groupe': (getattr(e, 'groupe', '') or ''),
                'display_formation': display_formation,
                'formateur': organisateur_id,
                'formateur_name': formateur_name,
            })
    except Exception:
        # swallow to avoid breaking the API, but keep a record in server logs
        import traceback
        print('planning_events_api: error while listing CalendarEvent:', traceback.format_exc())

    # Also include Session records that have horaire_debut/fin and date_debut in the week
    try:
        sessions_qs = Session.objects.filter(date_debut__gte=sunday, date_debut__lt=(sunday + timezone.timedelta(days=7)))
        for s in sessions_qs:
            if s.horaire_debut:
                sd = datetime.combine(s.date_debut, s.horaire_debut)
                ed = None
                if s.horaire_fin:
                    ed = datetime.combine(s.date_debut, s.horaire_fin)
                # make aware
                try:
                    sd = timezone.make_aware(sd)
                    if ed:
                        ed = timezone.make_aware(ed)
                except Exception:
                    pass
                # prepare display names with fallbacks
                session_title = str(s.formation) if s.formation else f'Session {s.id}'
                session_formation_name = (str(s.formation) if s.formation else '')
                session_formateur_name = (str(s.formateur) if s.formateur else '')
                display_formation = session_formation_name or session_title
                display_formateur = session_formateur_name or ''
                evs.append({
                    'id': f'session-{s.id}',
                    'title': session_title,
                    'start': sd.isoformat(),
                    'end': ed.isoformat() if ed else None,
                    'type': 'session',
                    'formation': (s.formation.id if s.formation else None),
                    'formation_name': session_formation_name,
                    'display_formation': display_formation,
                    'formateur': (s.formateur.id if s.formateur else None),
                    'formateur_name': session_formateur_name,
                    'display_formateur': display_formateur,
                    'salle': (s.salle.id if s.salle else None),
                    'salle_name': (str(s.salle) if s.salle else ''),
                    'groupe_name': getattr(s, 'groupe', '')
                })
    except Exception:
        pass

    return JsonResponse({'success': True, 'events': evs, 'week_start': sunday.isoformat(), 'debug_week_start': str(sunday)})


def planning_day_api(request):
    """Return events for a specific date and compute room availability for that date.
    Query params:
      - date: YYYY-MM-DD (preferred)
      - jour: accepts french weekday name (e.g. 'lundi') to refer to that day in the current week
    """
    date_str = request.GET.get('date') or request.GET.get('jour')
    try:
        if not date_str:
            return JsonResponse({'success': False, 'error': 'Missing date'}, status=400)
        # try ISO date first
        try:
            target = datetime.fromisoformat(date_str).date()
        except Exception:
            # accept french weekday names mapping to current week
            day_map = {'lundi':0,'mardi':1,'mercredi':2,'jeudi':3,'vendredi':4,'samedi':5,'dimanche':6}
            low = date_str.strip().lower()
            today = timezone.localdate()
            if low in day_map:
                # compute this week's target day (Monday=0). Our stored sessions use calendar dates.
                # find Monday of current week then add offset
                # use isoweekday() Monday=1..Sunday=7
                mon = today - timezone.timedelta(days=today.isoweekday()-1)
                target = mon + timezone.timedelta(days=day_map[low])
            else:
                return JsonResponse({'success': False, 'error': 'Invalid date or day name'}, status=400)

        # gather events and sessions for target date
        evs = []
        try:
            q_events = CalendarEvent.objects.filter(start_datetime__date=target)
            for e in q_events:
                evs.append({
                    'id': e.id,
                    'title': e.titre,
                    'start': e.start_datetime.isoformat(),
                    'end': e.end_datetime.isoformat() if e.end_datetime else None,
                    'salle_id': getattr(e, 'salle_id', None),
                    'salle_name': getattr(e, 'salle_name', '') or (Salle.objects.filter(id=getattr(e,'salle_id',None)).values_list('nom',flat=True).first() or ''),
                    'type': 'calendar_event',
                })
        except Exception:
            pass

        try:
            sessions_qs = Session.objects.filter(date_debut=target)
            for s in sessions_qs:
                sd = datetime.combine(s.date_debut, s.horaire_debut) if s.horaire_debut else datetime.combine(s.date_debut, datetime.min.time())
                ed = datetime.combine(s.date_debut, s.horaire_fin) if s.horaire_fin else (sd + timezone.timedelta(hours=1))
                try:
                    sd = timezone.make_aware(sd)
                    ed = timezone.make_aware(ed)
                except Exception:
                    pass
                evs.append({
                    'id': f'session-{s.id}',
                    'title': str(s.formation) if s.formation else f'Session {s.id}',
                    'start': sd.isoformat(),
                    'end': ed.isoformat() if ed else None,
                    'salle_id': s.salle.id if s.salle else None,
                    'salle_name': str(s.salle) if s.salle else '',
                    'type': 'session',
                })
        except Exception:
            pass

        # build per-salle occupied intervals
        salles = {}
        for ev in evs:
            sid = ev.get('salle_id')
            sname = ev.get('salle_name') or ''
            if sid is None:
                # try to skip events without a salle
                continue
            try:
                start_dt = datetime.fromisoformat(ev.get('start'))
            except Exception:
                continue
            try:
                end_dt = datetime.fromisoformat(ev.get('end')) if ev.get('end') else (start_dt + timezone.timedelta(hours=1))
            except Exception:
                end_dt = start_dt + timezone.timedelta(hours=1)
            lst = salles.setdefault(sid, {'salle_id': sid, 'salle_name': sname, 'occupied': []})
            lst['occupied'].append((start_dt.time(), end_dt.time()))

        # compute free slots for each salle between 08:00 and 24:00
        def merge_intervals(intervals):
            # intervals: list of (time, time)
            if not intervals: return []
            ints = sorted(intervals, key=lambda x: x[0])
            merged = [list(ints[0])]
            for a,b in ints[1:]:
                if a <= merged[-1][1]:
                    if b > merged[-1][1]: merged[-1][1] = b
                else:
                    merged.append([a,b])
            return merged

        from datetime import time as dt_time
        DAY_START = dt_time(8,0)
        DAY_END = dt_time(23,59)
        availability = []
        for sid, info in salles.items():
            occ = merge_intervals(info.get('occupied', []))
            free = []
            cur = DAY_START
            for a,b in occ:
                if a > cur:
                    free.append((cur, a))
                cur = max(cur, b)
            if cur < DAY_END:
                free.append((cur, DAY_END))
            availability.append({'salle_id': sid, 'salle_name': info.get('salle_name',''), 'free': [(t[0].strftime('%H:%M'), t[1].strftime('%H:%M')) for t in free], 'occupied': [(t[0].strftime('%H:%M'), t[1].strftime('%H:%M')) for t in occ]})

        return JsonResponse({'success': True, 'date': target.isoformat(), 'events': evs, 'availability': availability})
    except Exception as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=500)


def planning_room_api(request):
    """Return availability for a given room.
    Query params:
      - salle_id or salle_name (required)
      - date: optional YYYY-MM-DD. If provided, returns availability for that date, otherwise returns next 7 days.
    """
    sid = request.GET.get('salle_id') or request.GET.get('salle')
    sname = request.GET.get('salle_name')
    date_str = request.GET.get('date')
    try:
        salle_obj = None
        if sid:
            try:
                salle_obj = Salle.objects.filter(id=int(sid)).first()
            except Exception:
                salle_obj = Salle.objects.filter(nom__iexact=sid).first()
        elif sname:
            salle_obj = Salle.objects.filter(nom__iexact=sname).first()
        if not salle_obj:
            return JsonResponse({'success': False, 'error': 'Salle not found'}, status=404)

        dates = []
        if date_str:
            try:
                d0 = datetime.fromisoformat(date_str).date()
                dates = [d0]
            except Exception:
                return JsonResponse({'success': False, 'error': 'Invalid date'}, status=400)
        else:
            today = timezone.localdate()
            dates = [today + timezone.timedelta(days=i) for i in range(0,7)]

        results = []
        for d in dates:
            busy = []
            try:
                q_events = CalendarEvent.objects.filter(start_datetime__date=d).filter(Q(salle_id=salle_obj.id) | Q(salle_name__iexact=salle_obj.nom))
                for e in q_events:
                    st = e.start_datetime
                    en = e.end_datetime or (st + timezone.timedelta(hours=1))
                    busy.append((st.time(), en.time(), 'calendar_event', e.titre))
            except Exception:
                pass
            try:
                sessions_qs = Session.objects.filter(date_debut=d).filter(Q(salle_id=salle_obj.id) | Q(salle__nom__iexact=salle_obj.nom))
                for s in sessions_qs:
                    st = datetime.combine(s.date_debut, s.horaire_debut) if s.horaire_debut else datetime.combine(s.date_debut, datetime.min.time())
                    en = datetime.combine(s.date_debut, s.horaire_fin) if s.horaire_fin else (st + timezone.timedelta(hours=1))
                    try:
                        st = timezone.make_aware(st)
                        en = timezone.make_aware(en)
                    except Exception:
                        pass
                    busy.append((st.time(), en.time(), 'session', str(s.formation) if s.formation else f'Session {s.id}'))
            except Exception:
                pass

            # merge busy intervals
            intervals = sorted([(b[0], b[1]) for b in busy], key=lambda x: x[0])
            merged = []
            for a,b in intervals:
                if not merged:
                    merged.append([a,b])
                else:
                    if a <= merged[-1][1]:
                        if b > merged[-1][1]: merged[-1][1] = b
                    else:
                        merged.append([a,b])

            from datetime import time as dt_time
            DAY_START = dt_time(8,0)
            DAY_END = dt_time(23,59)
            free = []
            cur = DAY_START
            for a,b in merged:
                if a > cur:
                    free.append((cur,a))
                cur = max(cur,b)
            if cur < DAY_END:
                free.append((cur, DAY_END))

            results.append({'date': d.isoformat(), 'busy': [(t[0].strftime('%H:%M'), t[1].strftime('%H:%M')) for t in merged], 'free': [(t[0].strftime('%H:%M'), t[1].strftime('%H:%M')) for t in free]})

        return JsonResponse({'success': True, 'salle_id': salle_obj.id, 'salle_name': salle_obj.nom, 'results': results})
    except Exception as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=500)


@csrf_exempt
def add_planning_event(request):
    """Create a CalendarEvent from JSON payload.
    Expects JSON: { title, start (ISO), end (ISO optional) }
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Invalid method'}, status=405)

    try:
        payload = json.loads(request.body.decode('utf-8'))
    except Exception:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)

    title = payload.get('title') or payload.get('titre') or 'Nouvel événement'
    start_iso = payload.get('start')
    end_iso = payload.get('end')
    if not start_iso:
        return JsonResponse({'success': False, 'error': 'Missing start'}, status=400)

    try:
        start_dt = datetime.fromisoformat(start_iso)
        end_dt = datetime.fromisoformat(end_iso) if end_iso else None
        try:
            start_dt = timezone.make_aware(start_dt)
            if end_dt:
                end_dt = timezone.make_aware(end_dt)
        except Exception:
            pass

        # create base event
        ev = CalendarEvent.objects.create(titre=title, start_datetime=start_dt, end_datetime=end_dt)

        # optional relational/name fields
        formation_payload = payload.get('formation') or payload.get('formation_id')
        formation_name_payload = payload.get('formation_name') or payload.get('display_formation')
        formateur_payload = payload.get('formateur') or payload.get('formateur_id') or payload.get('organisateur') or payload.get('organisateur_id')
        formateur_name_payload = payload.get('formateur_name') or payload.get('display_formateur')
        salle_payload = payload.get('salle') or payload.get('salle_id')
        salle_name_payload = payload.get('salle_name') or payload.get('salle_label')
        groupe_payload = payload.get('groupe') or payload.get('groupe_name')

        # formation
        if formation_payload:
            try:
                fid = int(formation_payload)
                ev.formation_id = fid
                try:
                    fobj = Formation.objects.filter(id=fid).values_list('nom', flat=True).first()
                    if fobj:
                        ev.formation_name = fobj
                except Exception:
                    pass
            except Exception:
                ev.formation_name = formation_name_payload or str(formation_payload)
        elif formation_name_payload:
            ev.formation_name = formation_name_payload

        # formateur
        if formateur_payload:
            try:
                fid = int(formateur_payload)
                ev.organisateur_id = fid
                try:
                    fo = Enseignant.objects.filter(id=fid).first()
                    if fo:
                        ev.formateur_name = str(fo)
                    else:
                        u = Utilisateur.objects.filter(id=fid).first()
                        if u:
                            ev.formateur_name = f"{u.prenom or ''} {u.nom or ''}".strip()
                except Exception:
                    pass
            except Exception:
                ev.formateur_name = formateur_name_payload or str(formateur_payload)
        elif formateur_name_payload:
            ev.formateur_name = formateur_name_payload

        # salle
        if salle_payload:
            try:
                sid = int(salle_payload)
                ev.salle_id = sid
                try:
                    sname = Salle.objects.filter(id=sid).values_list('nom', flat=True).first()
                    if sname:
                        ev.salle_name = sname
                except Exception:
                    pass
            except Exception:
                ev.salle_name = salle_name_payload or str(salle_payload)
        elif salle_name_payload:
            ev.salle_name = salle_name_payload

        # groupe: resolve id -> name when possible, otherwise store provided string
        if groupe_payload is not None:
            try:
                try:
                    gid = int(groupe_payload)
                    gobj = Groupe.objects.filter(id=gid).first()
                    if gobj and getattr(gobj, 'nom', None):
                        ev.groupe = gobj.nom
                    else:
                        ev.groupe = str(groupe_payload)
                except Exception:
                    ev.groupe = str(groupe_payload)
            except Exception:
                ev.groupe = ''

        ev.save()

        # Handle multi-week repetition: create additional events on same weekday/time for the next N weeks
        created_ids = [ev.id]
        try:
            # normalize repeat flag (accept boolean true/false or '1'/'0' strings)
            repeat_flag = payload.get('repeat')
            if isinstance(repeat_flag, str):
                repeat_flag = repeat_flag.lower() in ('1', 'true', 'yes')
            else:
                repeat_flag = bool(repeat_flag)
            repeat_weeks = payload.get('repeat_weeks')
            try:
                repeat_count = int(repeat_weeks) if repeat_weeks is not None else 0
            except Exception:
                repeat_count = 0

            # normalize to non-negative
            if repeat_flag and repeat_count > 0:
                from datetime import timedelta
                for i in range(1, repeat_count+1):
                    try:
                        new_start = ev.start_datetime + timedelta(days=7*i) if ev.start_datetime else None
                        new_end = ev.end_datetime + timedelta(days=7*i) if ev.end_datetime else None
                        new_ev = CalendarEvent.objects.create(
                            titre=ev.titre,
                            start_datetime=new_start,
                            end_datetime=new_end,
                            formation_id=ev.formation_id,
                            formation_name=ev.formation_name,
                            organisateur_id=ev.organisateur_id,
                            formateur_name=ev.formateur_name,
                            salle_id=ev.salle_id,
                            salle_name=ev.salle_name,
                            groupe=(ev.groupe or ''),
                        )
                        created_ids.append(new_ev.id)
                    except Exception:
                        # non-fatal: continue creating other repeats
                        continue
        except Exception:
            pass

        return JsonResponse({'success': True, 'event': {'id': ev.id, 'title': ev.titre, 'start': ev.start_datetime.isoformat(), 'end': ev.end_datetime.isoformat() if ev.end_datetime else None, 'formation_id': ev.formation_id, 'formation_name': ev.formation_name, 'formateur_id': ev.organisateur_id, 'formateur_name': ev.formateur_name, 'salle': ev.salle_id, 'salle_name': ev.salle_name, 'groupe': (ev.groupe or '')}, 'created_ids': created_ids})
    except Exception as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=500)


@csrf_exempt
def edit_planning_event(request):
    """Edit an existing CalendarEvent. Accepts JSON payload with 'id' and same fields as add_planning_event."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Invalid method'}, status=405)
    try:
        payload = json.loads(request.body.decode('utf-8'))
    except Exception:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
    ev_id = payload.get('id')
    if not ev_id:
        return JsonResponse({'success': False, 'error': 'Missing id'}, status=400)
    try:
        # find event
        try:
            iid = int(ev_id)
            ev = CalendarEvent.objects.filter(id=iid).first()
        except Exception:
            ev = CalendarEvent.objects.filter(id=ev_id).first()
        if not ev:
            return JsonResponse({'success': False, 'error': 'Not found'}, status=404)

        # update common fields
        title = payload.get('title') or payload.get('titre')
        start_iso = payload.get('start')
        end_iso = payload.get('end')
        if start_iso:
            try:
                sd = datetime.fromisoformat(start_iso)
                try: sd = timezone.make_aware(sd)
                except Exception: pass
                ev.start_datetime = sd
            except Exception:
                pass
        if end_iso:
            try:
                ed = datetime.fromisoformat(end_iso)
                try: ed = timezone.make_aware(ed)
                except Exception: pass
                ev.end_datetime = ed
            except Exception:
                ev.end_datetime = None
        if title:
            ev.titre = title

        # formation
        formation_payload = payload.get('formation') or payload.get('formation_id')
        formation_name_payload = payload.get('formation_name') or payload.get('display_formation')
        if formation_payload:
            try:
                fid = int(formation_payload)
                ev.formation_id = fid
                try:
                    fobj = Formation.objects.filter(id=fid).values_list('nom', flat=True).first()
                    if fobj: ev.formation_name = fobj
                except Exception:
                    pass
            except Exception:
                if formation_name_payload:
                    ev.formation_name = formation_name_payload
                else:
                    ev.formation_name = str(formation_payload)
        elif formation_name_payload:
            ev.formation_name = formation_name_payload

        # formateur
        formateur_payload = payload.get('formateur') or payload.get('formateur_id') or payload.get('organisateur') or payload.get('organisateur_id')
        formateur_name_payload = payload.get('formateur_name') or payload.get('display_formateur')
        if formateur_payload:
            try:
                fid = int(formateur_payload)
                ev.organisateur_id = fid
                try:
                    fo = Enseignant.objects.filter(id=fid).first()
                    if fo: ev.formateur_name = str(fo)
                    else:
                        u = Utilisateur.objects.filter(id=fid).first()
                        if u: ev.formateur_name = f"{u.prenom or ''} {u.nom or ''}".strip()
                except Exception:
                    pass
            except Exception:
                if formateur_name_payload:
                    ev.formateur_name = formateur_name_payload
                else:
                    ev.formateur_name = str(formateur_payload)
        elif formateur_name_payload:
            ev.formateur_name = formateur_name_payload

        # salle
        salle_payload = payload.get('salle') or payload.get('salle_id')
        salle_name_payload = payload.get('salle_name') or payload.get('salle_label')
        if salle_payload:
            try:
                sid = int(salle_payload)
                ev.salle_id = sid
                try:
                    sname = Salle.objects.filter(id=sid).values_list('nom', flat=True).first()
                    if sname: ev.salle_name = sname
                except Exception:
                    pass
            except Exception:
                if salle_name_payload:
                    ev.salle_name = salle_name_payload
                else:
                    ev.salle_name = str(salle_payload)
        elif salle_name_payload:
            ev.salle_name = salle_name_payload

        # groupe handling for edits
        groupe_payload = payload.get('groupe') or payload.get('groupe_name')
        if groupe_payload is not None:
            try:
                try:
                    gid = int(groupe_payload)
                    gobj = Groupe.objects.filter(id=gid).first()
                    if gobj and getattr(gobj, 'nom', None):
                        ev.groupe = gobj.nom
                    else:
                        ev.groupe = str(groupe_payload)
                except Exception:
                    ev.groupe = str(groupe_payload)
            except Exception:
                ev.groupe = ''

        ev.save()
        return JsonResponse({'success': True, 'event': {'id': ev.id, 'title': ev.titre, 'start': ev.start_datetime.isoformat(), 'end': ev.end_datetime.isoformat() if ev.end_datetime else None, 'formation_id': ev.formation_id, 'formation_name': ev.formation_name, 'formateur_id': ev.organisateur_id, 'formateur_name': ev.formateur_name, 'salle': ev.salle_id, 'salle_name': ev.salle_name, 'groupe': (ev.groupe or '')}})
    except Exception as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=500)


@csrf_exempt
def delete_planning_event(request):
    """Delete a CalendarEvent by id. Accepts POST JSON: { id: <int> }"""
    if request.method not in ('POST', 'DELETE'):
        return JsonResponse({'success': False, 'error': 'Invalid method'}, status=405)
    try:
        payload = json.loads(request.body.decode('utf-8'))
    except Exception:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
    ev_id = payload.get('id')
    title = payload.get('title')
    start_iso = payload.get('start')
    if not ev_id and not (title and start_iso):
        return JsonResponse({'success': False, 'error': 'Missing id or title+start'}, status=400)
    try:
        ev = None
        if ev_id:
            try:
                # ensure numeric id where possible
                iid = int(ev_id)
                ev = CalendarEvent.objects.filter(id=iid).first()
            except Exception:
                # non-numeric id (e.g., session-123) -> ignore for CalendarEvent
                ev = CalendarEvent.objects.filter(id=ev_id).first()
        # fallback: try find by title+start
        if not ev and title and start_iso:
            try:
                sd = datetime.fromisoformat(start_iso)
                try:
                    sd = timezone.make_aware(sd)
                except Exception:
                    pass
                # match by exact start_datetime (or very close)
                ev = CalendarEvent.objects.filter(titre__iexact=title, start_datetime=sd).first()
            except Exception:
                ev = None
        if not ev:
            return JsonResponse({'success': False, 'error': 'Not found'}, status=404)
        ev.delete()
        return JsonResponse({'success': True})
    except Exception as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=500)


def examens_view(request):
    tests = Module.objects.count()
    # keep examens for compatibility but we will also provide inscriptions
    return render(request, 'examens.html', {'tests': tests})


def inscriptions_view(request):
    # List inscriptions with related etudiant and formation
    inscriptions = Inscription.objects.select_related('etudiant', 'formation').all().order_by('-date_inscription')
    students = Etudiant.objects.all()
    formations = Formation.objects.all()
    # provide formateurs list for selection (use Enseignant model)
    try:
        formateurs = Enseignant.objects.all()
    except Exception:
        # fallback to Utilisateur if Enseignant model not available
        formateurs = Utilisateur.objects.filter(statut__icontains='formateur')
    # annotate inscriptions with a simple 'groupe' extracted from remarques (if present)
    for ins in inscriptions:
        # prefer the stored model groupe value if present (DB column), otherwise
        # try to extract from remarques for backwards compatibility
        ins.formateur_name = ''
        try:
            if not getattr(ins, 'groupe', None):
                ins.groupe = ''
                if ins.remarques:
                    parts = [p.strip() for p in ins.remarques.split(';') if p.strip()]
                    for p in parts:
                        if p.startswith('groupe:'):
                            ins.groupe = p.split(':',1)[1]
                        if p.startswith('formateur:'):
                            try:
                                fid = int(p.split(':',1)[1])
                                # try Enseignant first, then Utilisateur
                                f = None
                                try:
                                    f = Enseignant.objects.filter(id=fid).first()
                                except Exception:
                                    f = None
                                if not f:
                                    f = Utilisateur.objects.filter(id=fid).first()
                                if f:
                                    ins.formateur_name = str(f)
                            except Exception:
                                pass
            else:
                # formateur name still may be in remarques
                if ins.remarques:
                    parts = [p.strip() for p in ins.remarques.split(';') if p.strip()]
                    for p in parts:
                        if p.startswith('formateur:'):
                            try:
                                fid = int(p.split(':',1)[1])
                                f = None
                                try:
                                    f = Enseignant.objects.filter(id=fid).first()
                                except Exception:
                                    f = None
                                if not f:
                                    f = Utilisateur.objects.filter(id=fid).first()
                                if f:
                                    ins.formateur_name = str(f)
                            except Exception:
                                pass
        except Exception:
            pass
    # build per-formation groupe options and mark disabled when full (capacity 16)
    import json
    formation_groups = {}
    DEFAULT_GROUP_CAPACITY = 16
    from .models import Groupe as GroupeModel
    for f in formations:
        opts = []
        try:
            groupes_qs = GroupeModel.objects.filter(formation=f).order_by('created_at')
        except Exception:
            groupes_qs = []
        for g in groupes_qs:
            try:
                cap = int(g.capacite) if g.capacite is not None else DEFAULT_GROUP_CAPACITY
            except Exception:
                cap = DEFAULT_GROUP_CAPACITY
            try:
                cnt = Inscription.objects.filter(formation=f, groupe=g).count()
            except Exception:
                cnt = 0
            opts.append({'id': g.id, 'nom': g.nom, 'disabled': cnt >= cap, 'count': cnt, 'capacity': cap})
        formation_groups[str(f.id)] = opts
    # build a sorted list of groupe names (strings) for select options
    groupe_options = sorted({
        (ins.groupe.nom if getattr(ins, 'groupe', None) and getattr(ins.groupe, 'nom', None) else '')
        for ins in inscriptions if getattr(ins, 'groupe', None) and (getattr(ins.groupe, 'nom', None) or '').strip()
    })
    # ensure each student has a photo_url attribute like in etudiants_view
    for s in students:
        try:
            s.photo_url = (settings.MEDIA_URL + s.photo) if s.photo else ''
        except Exception:
            s.photo_url = s.photo or ''

    # load simple school info (name + logo) from a JSON file if present
    school_name = ''
    school_logo = ''
    try:
        import os
        info_path = os.path.join(settings.BASE_DIR, 'school_info.json')
        if os.path.exists(info_path):
            with open(info_path, 'r', encoding='utf-8') as f:
                info = json.load(f)
                school_name = info.get('name', '')
                school_logo = info.get('logo', '')
    except Exception:
        school_name = ''
        school_logo = ''

    return render(request, 'inscriptions.html', {
        'inscriptions': inscriptions,
        'students': students,
        'formations': formations,
        'formateurs': formateurs,
        'groupe_options': groupe_options,
        'formation_groups_json': json.dumps(formation_groups),
        'school_name': school_name,
        'school_logo': school_logo,
    })


def sw_paiements(request):
    """Serve a small service-worker script for the paiements page from the project root.
    This lets the SW be registered with scope '/paiements/'.
    """
    try:
        import os
        sw_path = os.path.join(settings.BASE_DIR, 'Schoolapp', 'static', 'sw-paiements.js')
        with open(sw_path, 'rb') as f:
            content = f.read()
        response = HttpResponse(content, content_type='application/javascript')
        # Allow the service worker to claim the whole origin if necessary
        response['Service-Worker-Allowed'] = '/'
        return response
    except Exception:
        raise Http404()



def add_inscription(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Invalid method'}, status=405)
    # Accept either an etudiant id or a name/email typed
    etu_field = request.POST.get('etudiant')
    formation_field = request.POST.get('formation')
    if not etu_field:
        return JsonResponse({'success': False, 'error': 'Missing etudiant'}, status=400)
    # Resolve etudiant
    etu = None
    try:
        # try integer id
        etu_id = int(etu_field)
        etu = Etudiant.objects.filter(id=etu_id).first()
    except Exception:
        # try by email
        etu = Etudiant.objects.filter(email__iexact=etu_field).first()
        if not etu:
            # try by full name or partial
            parts = etu_field.strip().split()
            if len(parts) >= 2:
                etu = Etudiant.objects.filter(nom__iexact=parts[-1], prenom__iexact=' '.join(parts[:-1])).first()
            if not etu:
                etu = Etudiant.objects.filter(nom__icontains=etu_field).first()
    if not etu:
        return JsonResponse({'success': False, 'error': 'Etudiant introuvable'}, status=404)

    # Resolve formation by id or name
    formation = None
    if formation_field:
        try:
            formation_id = int(formation_field)
            formation = Formation.objects.filter(id=formation_id).first()
        except Exception:
            formation = Formation.objects.filter(nom__iexact=formation_field).first()

    if not formation:
        return JsonResponse({'success': False, 'error': 'Formation introuvable'}, status=404)

    # Create inscription if not exists
    try:
        # determine session and groupe (as a Groupe instance)
        session_val = request.POST.get('session') or 'session normale'
        groupe_field = request.POST.get('groupe') or None
        groupe_obj = None
        from .models import Groupe as GroupeModel
        DEFAULT_GROUP_CAPACITY = 16
        # If client provided a groupe id or name, try resolving to a Groupe instance
        if groupe_field:
            try:
                # try id first
                gid = int(groupe_field)
                groupe_obj = GroupeModel.objects.filter(id=gid, formation=formation).first()
            except Exception:
                # try by name/nom
                groupe_obj = GroupeModel.objects.filter(nom__iexact=groupe_field, formation=formation).first()
            if groupe_obj:
                try:
                    cap = int(groupe_obj.capacite) if groupe_obj.capacite is not None else DEFAULT_GROUP_CAPACITY
                except Exception:
                    cap = DEFAULT_GROUP_CAPACITY
                try:
                    cnt = Inscription.objects.filter(formation=formation, groupe=groupe_obj).count()
                except Exception:
                    cnt = 0
                if cnt >= cap:
                    return JsonResponse({'success': False, 'error': f'Groupe {groupe_obj.nom} est complet'}, status=400)

        # If groupe not provided or not resolved, auto-assign the first Groupe with available capacity
        if not groupe_obj:
            try:
                groupes_qs = GroupeModel.objects.filter(formation=formation).order_by('created_at')
            except Exception:
                groupes_qs = []
            for g in groupes_qs:
                try:
                    cap = int(g.capacite) if g.capacite is not None else DEFAULT_GROUP_CAPACITY
                except Exception:
                    cap = DEFAULT_GROUP_CAPACITY
                try:
                    cnt = Inscription.objects.filter(formation=formation, groupe=g).count()
                except Exception:
                    cnt = 0
                if cnt < cap:
                    groupe_obj = g
                    break

        # if still no groupe chosen, no available groups for this formation
        if not groupe_obj:
            return JsonResponse({'success': False, 'error': 'Aucun groupe disponible pour cette formation (tous complets ou aucun groupe défini)'}, status=400)

        inscription, created = Inscription.objects.get_or_create(
            etudiant=etu,
            formation=formation,
            defaults={
                'statut': request.POST.get('statut', 'inscrit'),
                'prix_total': request.POST.get('prix_total') or None,
                'session': session_val,
                'groupe': groupe_obj,
                'ecole': request.POST.get('ecole') or None,
                'date_inscription': request.POST.get('date_inscription') or None,
            }
        )
        if not created:
            return JsonResponse({'success': False, 'error': 'Inscription existe déjà'}, status=400)
    except Exception as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=500)

    # Build a JSON-friendly representation of the created inscription and return it
    try:
        ins = inscription
        formateur_id = None
        formateur_name = ''
        # try to extract formateur token from remarques if present
        try:
            if ins.remarques:
                parts = [p.strip() for p in ins.remarques.split(';') if p.strip()]
                for p in parts:
                    if p.startswith('formateur:'):
                        try:
                            fid = int(p.split(':', 1)[1])
                            formateur_id = fid
                            f = Enseignant.objects.filter(id=fid).first() or Utilisateur.objects.filter(id=fid).first()
                            if f:
                                formateur_name = str(f)
                        except Exception:
                            pass
        except Exception:
            pass

        # If the client supplied a formateur field in the POST, prefer it and persist
        try:
            formateur_field = request.POST.get('formateur')
            if formateur_field:
                try:
                    fid = int(formateur_field)
                    f = Enseignant.objects.filter(id=fid).first() or Utilisateur.objects.filter(id=fid).first()
                    if f:
                        formateur_id = fid
                        formateur_name = str(f)
                        # persist token in remarques for backward compatibility
                        try:
                            parts = [p for p in (ins.remarques or '').split(';') if p and not p.startswith('formateur:')]
                            parts.append(f'formateur:{int(formateur_field)}')
                            ins.remarques = ';'.join(parts)
                            ins.save()
                        except Exception:
                            # non-fatal
                            pass
                except Exception:
                    pass
        except Exception:
            pass

        # Build response payload with the same keys used by edit_inscription
        etu = getattr(ins, 'etudiant', None)
        etudiant_nom = ''
        etudiant_email = ''
        etudiant_phone = ''
        etudiant_photo = ''
        try:
            if etu:
                etudiant_nom = f"{getattr(etu, 'nom', '')} {getattr(etu, 'prenom', '')}".strip()
                etudiant_email = getattr(etu, 'email', '') or ''
                etudiant_phone = getattr(etu, 'telephone', '') or ''
                try:
                    if getattr(etu, 'photo', None):
                        etudiant_photo = default_storage.url(etu.photo)
                    else:
                        etudiant_photo = ''
                except Exception:
                    try:
                        etudiant_photo = (settings.MEDIA_URL + etu.photo) if getattr(etu, 'photo', None) else ''
                    except Exception:
                        etudiant_photo = getattr(etu, 'photo', '') or ''
        except Exception:
            pass

        ins_date = None
        try:
            if getattr(ins, 'date_inscription', None):
                ins_date = ins.date_inscription.isoformat()
        except Exception:
            ins_date = None

        ins_data = {
            'id': ins.id,
            'etudiant_id': ins.etudiant.id if getattr(ins, 'etudiant', None) else None,
            'etudiant_nom': etudiant_nom,
            'etudiant_email': etudiant_email,
            'etudiant_phone': etudiant_phone,
            'etudiant_photo': etudiant_photo,
            'formation_id': ins.formation.id if getattr(ins, 'formation', None) else None,
            'formation': ins.formation.nom if getattr(ins, 'formation', None) else '',
            'groupe': (ins.groupe.nom if getattr(ins, 'groupe', None) and getattr(ins.groupe, 'nom', None) else ''),
            'session': ins.session or '',
            'statut': ins.statut or '',
            'prix_total': str(ins.prix_total) if ins.prix_total is not None else None,
            'date_inscription': ins_date,
            'formateur_id': formateur_id,
            'formateur_name': formateur_name,
        }
        # New inscription should reset any explicit 'paiement_regle' marker for the student
        try:
            if etu:
                rm = (etu.remarques or '')
                if 'paiement_regle:1' in rm:
                    # remove token
                    parts = [p.strip() for p in rm.split(';') if p.strip() and p.strip() != 'paiement_regle:1']
                    etu.remarques = ';'.join(parts) if parts else None
                    etu.save()
        except Exception:
            pass
        return JsonResponse({'success': True, 'inscription': ins_data})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


def edit_inscription(request):
    """Edit an existing inscription via AJAX POST.
    Expected POST fields: id (required), optional: formation (id), groupe, statut, prix_total, session, formateur (id), etudiant (id).
    This updates the Inscription, persists groupe/formateur tokens into `remarques` for backward compatibility,
    and returns a JSON summary including formateur_id and formateur_name.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Invalid method'}, status=405)
    ins_id = request.POST.get('id')
    if not ins_id:
        return JsonResponse({'success': False, 'error': 'Missing id'}, status=400)
    try:
        ins = Inscription.objects.select_related('etudiant', 'formation').filter(id=int(ins_id)).first()
    except Exception:
        ins = None
    if not ins:
        return JsonResponse({'success': False, 'error': 'Inscription introuvable'}, status=404)

    changed = False
    # formation
    formation_field = request.POST.get('formation')
    if formation_field:
        try:
            formation = Formation.objects.filter(id=int(formation_field)).first()
            if formation and (not ins.formation or formation.id != ins.formation_id):
                ins.formation = formation
                changed = True
        except Exception:
            pass

    # other simple fields (handle 'groupe' separately so we persist FK correctly)
    for fld in ('statut', 'session'):
        if fld in request.POST:
            val = request.POST.get(fld)
            if val is None:
                continue
            if str(getattr(ins, fld) or '') != str(val):
                setattr(ins, fld, val)
                changed = True

    # groupe field: resolve to Groupe instance by id or name (respect current inscription formation)
    if 'groupe' in request.POST:
        try:
            val = request.POST.get('groupe')
            from .models import Groupe as GroupeModel
            # if empty string, clear the groupe
            if val is None or (isinstance(val, str) and val.strip() == ''):
                if getattr(ins, 'groupe', None) is not None:
                    ins.groupe = None
                    changed = True
            else:
                g_obj = None
                try:
                    gid = int(val)
                    g_obj = GroupeModel.objects.filter(id=gid, formation=ins.formation).first()
                except Exception:
                    # try by name (case-insensitive)
                    g_obj = GroupeModel.objects.filter(nom__iexact=val, formation=ins.formation).first()
                if g_obj:
                    if not ins.groupe or (getattr(ins.groupe, 'id', None) != g_obj.id):
                        ins.groupe = g_obj
                        changed = True
                # if groupe not found, leave unchanged (do not assign raw string to FK)
        except Exception:
            pass

        # ecole field (optional)
        if 'ecole' in request.POST:
            try:
                val = request.POST.get('ecole') or ''
                if str(getattr(ins, 'ecole') or '') != str(val):
                    ins.ecole = val or None
                    changed = True
            except Exception:
                pass

    # date_inscription handling (optional)
    if 'date_inscription' in request.POST:
        try:
            civ = request.POST.get('date_inscription') or None
            if civ:
                # try to parse YYYY-MM-DD
                from datetime import datetime
                try:
                    parsed = datetime.strptime(civ, '%Y-%m-%d').date()
                except Exception:
                    # fallback: try ISO parse
                    try:
                        parsed = datetime.fromisoformat(civ).date()
                    except Exception:
                        parsed = None
                if parsed is not None and getattr(ins, 'date_inscription', None) != parsed:
                    ins.date_inscription = parsed
                    changed = True
            else:
                # clear date if empty
                if getattr(ins, 'date_inscription', None) is not None:
                    ins.date_inscription = None; changed = True
        except Exception:
            pass

    # prix_total numeric
    if 'prix_total' in request.POST:
        val = request.POST.get('prix_total')
        try:
            valf = float(val) if val not in (None, '') else None
            if valf is None:
                if ins.prix_total is not None:
                    ins.prix_total = None; changed = True
            else:
                if float(ins.prix_total or 0) != float(valf):
                    ins.prix_total = valf; changed = True
        except Exception:
            pass

    # formateur handling: persist token in remarques for compatibility and compute name
    formateur_field = request.POST.get('formateur')
    if formateur_field:
        try:
            # try Enseignant first then Utilisateur
            f = Enseignant.objects.filter(id=int(formateur_field)).first()
            if not f:
                f = Utilisateur.objects.filter(id=int(formateur_field)).first()
            if f:
                # append token into remarques
                rem = ins.remarques or ''
                # remove any previous formateur: tokens
                parts = [p for p in rem.split(';') if p and not p.startswith('formateur:')]
                parts.append(f'formateur:{int(formateur_field)}')
                ins.remarques = ';'.join(parts)
                changed = True
        except Exception:
            pass

    # persist changes
    try:
        if changed:
            ins.save()
    except Exception as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=500)

    # compute formateur name for response
    formateur_name = ''
    formateur_id = None
    try:
        if formateur_field:
            formateur_id = int(formateur_field)
            f = Enseignant.objects.filter(id=formateur_id).first() or Utilisateur.objects.filter(id=formateur_id).first()
            if f:
                formateur_name = str(f)
    except Exception:
        formateur_name = ''

        # prepare etudiant display fields for client-side insertion
        etu = getattr(ins, 'etudiant', None)
        etudiant_nom = ''
        etudiant_email = ''
        etudiant_phone = ''
        etudiant_photo = ''
        try:
            if etu:
                etudiant_nom = f"{getattr(etu, 'nom', '')} {getattr(etu, 'prenom', '')}".strip()
                etudiant_email = getattr(etu, 'email', '') or ''
                etudiant_phone = getattr(etu, 'telephone', '') or ''
                try:
                    if getattr(etu, 'photo', None):
                        etudiant_photo = default_storage.url(etu.photo)
                    else:
                        etudiant_photo = ''
                except Exception:
                    try:
                        etudiant_photo = (settings.MEDIA_URL + etu.photo) if getattr(etu, 'photo', None) else ''
                    except Exception:
                        etudiant_photo = getattr(etu, 'photo', '') or ''
        except Exception:
            pass

        ins_date = None
        try:
            if getattr(ins, 'date_inscription', None):
                ins_date = ins.date_inscription.isoformat()
        except Exception:
            ins_date = None

        ins_data = {
            'id': ins.id,
            'etudiant_id': ins.etudiant.id if getattr(ins, 'etudiant', None) else None,
            'etudiant_nom': etudiant_nom,
            'etudiant_email': etudiant_email,
            'etudiant_phone': etudiant_phone,
            'etudiant_photo': etudiant_photo,
            'formation_id': ins.formation.id if getattr(ins, 'formation', None) else None,
            'formation': ins.formation.nom if getattr(ins, 'formation', None) else '',
            'groupe': (ins.groupe.nom if getattr(ins, 'groupe', None) and getattr(ins.groupe, 'nom', None) else ''),
            'session': ins.session or '',
            'statut': ins.statut or '',
            'prix_total': str(ins.prix_total) if ins.prix_total is not None else None,
            'date_inscription': ins_date,
            'ecole': ins.ecole or '',
            'formateur_id': formateur_id,
            'formateur_name': formateur_name,
        }

    return JsonResponse({'success': True, 'inscription': ins_data})



def inscriptions_delete(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Invalid method'}, status=405)
    try:
        ins_id = int(request.POST.get('id') or request.POST.get('_id'))
    except Exception:
        return JsonResponse({'success': False, 'error': 'Missing id'}, status=400)
    ins = Inscription.objects.filter(id=ins_id).first()
    if not ins:
        return JsonResponse({'success': False, 'error': 'Not found'}, status=404)
    try:
        etu = ins.etudiant
        ins.delete()
        # optionally update student statut if they have no other active inscriptions
        try:
            remaining = Inscription.objects.filter(etudiant=etu, statut__iexact='inscrit').exists()
            if not remaining:
                etu.statut = 'non_inscrit'
                etu.save()
        except Exception:
            pass
        return JsonResponse({'success': True, 'deleted': ins_id})
    except Exception as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=500)




def finances_view(request):
    # total paiements étudiants
    paiements_total = Paiement.objects.aggregate(total=Sum('montant'))['total'] or 0
    # total règlements enseignants
    from .models import ReglementEnseignants
    reglements_enseignants_total = ReglementEnseignants.objects.aggregate(total=Sum('montant'))['total'] or 0
    # supplier reglements model not implemented yet — use 0 as placeholder
    # supplier reglements model not implemented yet — approximate with charges (Charge model)
    try:
        from .models import Charge
        charges_total = Charge.objects.aggregate(total=Sum('montant'))['total'] or 0
    except Exception:
        charges_total = 0

    # total des versements des écoles (SchoolVersement)
    try:
        from .models import SchoolVersement
        school_versements_total = SchoolVersement.objects.aggregate(total=Sum('montant'))['total'] or 0
    except Exception:
        school_versements_total = 0

    # trésorerie (brut) = (paiements étudiants + versements écoles) - (charges + règlements enseignants)
    tresorerie = (paiements_total or 0) + (school_versements_total or 0) - ((charges_total or 0) + (reglements_enseignants_total or 0))

    # bénéfice net = paiements étudiants + versements des écoles (utilisé ailleurs)
    benefice_net = (paiements_total or 0) + (school_versements_total or 0)

    # top formations by revenue (for the donut segments and legend)
    # initialize defaults so templates never fail if DB is empty or an error occurs
    donut_segments = []
    legend_items = []
    try:
        top_qs = list(Paiement.objects.values('formation__nom').annotate(total=Sum('montant')).order_by('-total')[:6])
        # ensure at most 6 entries
        top6 = [{'formation': r.get('formation__nom') or 'Autres', 'total': float(r.get('total') or 0)} for r in top_qs]
        total_sum = sum([x['total'] for x in top6]) or float(paiements_total) or 0.0
        # fallback: if still zero, produce equal slices to avoid div by zero
        if total_sum == 0 and len(top6) == 0:
            top6 = []
            total_sum = 0.0

        # colors for the donut slices (at least 4 colors)
        colors = ['#4c51bf', '#38a169', '#d69e2e', '#a78bfa']
        donut_segments = []
        # prepare revenue list
        rev_list = top6
        slots = 4
        if total_sum == 0 or not rev_list:
            # no data -> 4 equal slices
            for idx in range(slots):
                donut_segments.append({'formation': f'Autres{idx+1}', 'total': 0.0, 'percent': 100.0 / slots, 'color': colors[idx % len(colors)]})
        else:
            # build initial percentages for available formations (up to slots)
            assigned = []
            for entry in rev_list[:slots]:
                val = float(entry['total'] or 0)
                pct = (val / total_sum * 100.0) if total_sum > 0 else 0.0
                assigned.append({'formation': entry['formation'], 'total': val, 'percent': pct})
            # fill to exactly 'slots' entries (placeholders have 0 total initially)
            slices = []
            for a in assigned:
                slices.append({'formation': a['formation'], 'total': a['total'], 'percent': a['percent']})
            while len(slices) < slots:
                slices.append({'formation': 'Autres', 'total': 0.0, 'percent': 0.0})

            # enforce a minimum visible percent for any slice so it appears on the donut
            MIN_VIS_PCT = 6.0
            for s in slices:
                if s['percent'] > 0 and s['percent'] < MIN_VIS_PCT:
                    s['percent'] = MIN_VIS_PCT
            # ensure placeholders that are zero still get MIN_VIS_PCT so they show up
            for s in slices:
                if s['percent'] == 0.0:
                    s['percent'] = MIN_VIS_PCT

            # normalize so total is 100%: subtract excess from the largest real slice if needed
            total_pct = sum(s['percent'] for s in slices)
            if total_pct != 100.0:
                diff = 100.0 - total_pct
                # find index of slice with maximum percent (prefer actual data over 'Autres')
                def score(si):
                    return (0 if si['formation'].startswith('Autres') else 1, si['percent'])
                largest_idx = max(range(len(slices)), key=lambda i: score(slices[i]))
                slices[largest_idx]['percent'] += diff

            # finalize donut_segments with computed percents
            for idx, s in enumerate(slices):
                donut_segments.append({'formation': s['formation'], 'total': s.get('total', 0.0), 'percent': s['percent'], 'color': colors[idx % len(colors)]})

            # Convert percent slices into explicit SVG arc path commands so each slice
            # is drawn independently (more robust than stroke-dasharray across browsers).
            try:
                import math
                cx, cy = 18.0, 18.0
                r = 15.9155
                start_angle = -math.pi / 2.0  # start at top (matches original circle path)
                for seg in donut_segments:
                    pct = float(seg.get('percent', 0.0))
                    # full circle special-case
                    if pct >= 99.9999:
                        # use the original full circle path
                        seg['arc_d'] = f"M{cx} {cy - r} a {r} {r} 0 0 1 0 {2*r} a {r} {r} 0 0 1 0 {-2*r}"
                        # advance start_angle by full circle
                        start_angle += 2 * math.pi
                        continue
                    angle = (pct / 100.0) * (2.0 * math.pi)
                    end_angle = start_angle + angle
                    x1 = cx + r * math.cos(start_angle)
                    y1 = cy + r * math.sin(start_angle)
                    x2 = cx + r * math.cos(end_angle)
                    y2 = cy + r * math.sin(end_angle)
                    large_arc = 1 if angle > math.pi else 0
                    # A rx ry x-axis-rotation large-arc-flag sweep-flag x y
                    d = f"M {x1:.4f} {y1:.4f} A {r:.4f} {r:.4f} 0 {large_arc} 1 {x2:.4f} {y2:.4f}"
                    seg['arc_d'] = d
                    start_angle = end_angle
            except Exception:
                # fallback: no arc data
                for seg in donut_segments:
                    seg['arc_d'] = None

            # legend: show first 4 formations with colors
            legend_items = []
            for i in range(min(4, len(donut_segments))):
                legend_items.append({'formation': donut_segments[i]['formation'], 'total': donut_segments[i].get('total', 0.0), 'color': donut_segments[i]['color']})
    except Exception:
        donut_segments = []
        legend_items = []

    invoices = Invoice.objects.count()
    # total charges
    try:
        charges_total = Charge.objects.aggregate(total=Sum('montant'))['total'] or 0
    except Exception:
        charges_total = 0

    # Investment outflows totals (ReglementEnseignants + Charge) for day / month / year
    try:
        from django.utils import timezone as dj_timezone
        now_dt = dj_timezone.now()
        # day
        day_start = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + dj_timezone.timedelta(days=1)
        r_day = ReglementEnseignants.objects.filter(date_reglement__gte=day_start.date(), date_reglement__lt=day_end.date()).aggregate(total=Sum('montant'))['total'] or 0
        c_day = Charge.objects.filter(date_paiement__gte=day_start.date(), date_paiement__lt=day_end.date()).aggregate(total=Sum('montant'))['total'] or 0
        invest_total_day = (r_day or 0) + (c_day or 0)

        # month (from first day of current month)
        month_start = now_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        r_month = ReglementEnseignants.objects.filter(date_reglement__gte=month_start.date()).aggregate(total=Sum('montant'))['total'] or 0
        c_month = Charge.objects.filter(date_paiement__gte=month_start.date()).aggregate(total=Sum('montant'))['total'] or 0
        invest_total_month = (r_month or 0) + (c_month or 0)

        # year (from first day of current year)
        year_start = now_dt.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        r_year = ReglementEnseignants.objects.filter(date_reglement__gte=year_start.date()).aggregate(total=Sum('montant'))['total'] or 0
        c_year = Charge.objects.filter(date_paiement__gte=year_start.date()).aggregate(total=Sum('montant'))['total'] or 0
        invest_total_year = (r_year or 0) + (c_year or 0)
    except Exception:
        invest_total_day = invest_total_month = invest_total_year = 0

    # Weekly stats for last 4 weeks (non-overlapping 7-day windows ending today)
    from datetime import date, timedelta
    today = date.today()
    weekly_stats = []
    # collect raw values to compute heights
    raw_values = []
    for i in range(4):
        end = today - timedelta(days=i * 7)
        start = end - timedelta(days=6)
        p_sum = Paiement.objects.filter(date_paiement__range=(start, end)).aggregate(total=Sum('montant'))['total'] or 0
        r_sum = ReglementEnseignants.objects.filter(date_reglement__range=(start, end)).aggregate(total=Sum('montant'))['total'] or 0
        c_sum = Charge.objects.filter(date_paiement__range=(start, end)).aggregate(total=Sum('montant'))['total'] or 0
        revenue = float(p_sum or 0)
        expense = float((r_sum or 0) + (c_sum or 0))
        weekly_stats.append({'label': f'S-{i+1}', 'start': start, 'end': end, 'revenue': revenue, 'expense': expense})
        raw_values.append(revenue)
        raw_values.append(expense)

    # Normalize heights for bars (max pixel height 160)
    max_val = max(raw_values) if raw_values else 0
    if not max_val or max_val == 0:
        max_val = 1.0
    for w in weekly_stats:
        w['rev_px'] = int((w['revenue'] / max_val) * 160)
        w['exp_px'] = int((w['expense'] / max_val) * 160)

    # minimal role check: detect if current user is 'Secretaire' to adjust UI (same logic as dashboard)
    try:
        uid = request.session.get('user_id') or None
        current_user = Utilisateur.objects.filter(id=uid).first() if uid else None
        role = (getattr(current_user, 'role', '') or '').strip().lower() if current_user else ''
        is_secretaire = role in ('secretaire', 'secrétaire')
    except Exception:
        is_secretaire = False

    # Compute per-school owed amounts based on inscriptions.ecole (exclude 'Genie Systeme')
    try:
        from collections import defaultdict
        ecoles = defaultdict(float)
        ecoles_counts = defaultdict(int)
        frais_ecoles_count = 0
        ins_qs = Inscription.objects.select_related('formation').filter(ecole__isnull=False).exclude(ecole__exact='').all()
        for ins in ins_qs:
            try:
                ecole_name = (ins.ecole or '').strip()
                if not ecole_name:
                    continue
                # skip internal school 'Genie Systeme' (case-insensitive)
                if 'genie' in ecole_name.lower() and 'system' in ecole_name.lower():
                    continue
                # get base price
                base = 0.0
                if getattr(ins, 'prix_total', None) is not None:
                    base = float(ins.prix_total or 0)
                elif getattr(ins, 'formation', None) and getattr(ins.formation, 'prix_etudiant', None) is not None:
                    base = float(ins.formation.prix_etudiant or 0)

                # deduction rules (fibre:3000, reseau/camera:2000)
                fname = (getattr(ins.formation, 'nom', '') or '').lower() if getattr(ins, 'formation', None) else ''
                if 'fibre' in fname:
                    # Jura School has a different consumable fee for FTTH
                    try:
                        if (ecole_name or '').strip().lower() == 'jura school':
                            deducted = 4500.0
                        else:
                            deducted = 3000.0
                    except Exception:
                        deducted = 3000.0
                elif 'reseau' in fname or 'camera' in fname or 'caméra' in fname:
                    deducted = 2000.0
                else:
                    deducted = 0.0

                # apply requested rule: split the remaining amount in two and
                # then add the fixed 'consommable' back to the school's share
                remaining = max(0.0, base - deducted)
                school_share = (remaining / 2.0) + deducted
                ecoles[ecole_name] += school_share
                ecoles_counts[ecole_name] += 1
                frais_ecoles_count += 1
            except Exception:
                continue
        ecoles_summary = dict(ecoles)
        ecoles_counts = dict(ecoles_counts)
        # JSON-friendly serializations for client-side embed
        try:
            import json as _json
            ecoles_summary_json = _json.dumps(ecoles_summary)
            ecoles_counts_json = _json.dumps(ecoles_counts)
        except Exception:
            ecoles_summary_json = '{}'
            ecoles_counts_json = '{}'
        frais_ecoles_total = sum(ecoles_summary.values())
        # subtract any payments already recorded for each school so the displayed
        # amount represents remaining due (frais - paiements). Payments created
        # via add_ecole_versement or add_ecole_paiements_bulk set Paiement.ecole.
        try:
            from django.db.models import Sum as _Sum
            from django.db.models import Sum as _Sum
            for ename in list(ecoles_summary.keys()):
                try:
                    paid_paiements = Paiement.objects.filter(ecole__iexact=ename).aggregate(total=_Sum('montant'))['total'] or 0
                    # include school-level versements recorded in SchoolVersement
                    try:
                        paid_school = SchoolVersement.objects.filter(ecole__iexact=ename).aggregate(total=_Sum('montant'))['total'] or 0
                    except Exception:
                        paid_school = 0
                    paid = (paid_paiements or 0) + (paid_school or 0)
                    ecoles_summary[ename] = float(max(0.0, ecoles_summary.get(ename, 0.0) - float(paid)))
                except Exception:
                    continue
            # recompute total after deduction
            frais_ecoles_total = sum(ecoles_summary.values())
            try:
                ecoles_summary_json = _json.dumps(ecoles_summary)
            except Exception:
                pass
        except Exception:
            pass
    except Exception:
        ecoles_summary = {}
        frais_ecoles_total = 0
        frais_ecoles_count = 0

    return render(request, 'finances.html', {
        'paiements_total': paiements_total,
    'reglements_enseignants_total': reglements_enseignants_total,
    'tresorerie': tresorerie,
    'benefice_net': benefice_net,
    'school_versements_total': school_versements_total,
    'top_formations': legend_items,
    'donut_segments': donut_segments,
        'charges_total': charges_total,
        'invest_total_day': invest_total_day,
        'invest_total_month': invest_total_month,
        'invest_total_year': invest_total_year,
        'weekly_stats': weekly_stats,
        'invoices': invoices,
        # small summary for "Frais d'écoles" card (best-effort: match charges whose type mentions 'frais' or 'ecole')
        'frais_ecoles_total': frais_ecoles_total if 'frais_ecoles_total' in locals() else 0,
        'frais_ecoles_count': frais_ecoles_count if 'frais_ecoles_count' in locals() else 0,
        # provide JSON strings for safe client-side embedding
        'ecoles_summary_json': ecoles_summary_json if 'ecoles_summary_json' in locals() else '{}',
        'ecoles_counts_json': ecoles_counts_json if 'ecoles_counts_json' in locals() else '{}',
        'is_secretaire': is_secretaire,
    })


def ecole_paiements_view(request):
    """Render a page listing external schools and, when a school is selected,
    show its students (inscriptions) and computed school-share for each.
    """
    from collections import defaultdict
    ecoles = defaultdict(float)
    ecoles_counts = defaultdict(int)
    ins_qs = Inscription.objects.select_related('formation', 'etudiant').filter(ecole__isnull=False).exclude(ecole__exact='').all()
    for ins in ins_qs:
        try:
            ecole_name = (ins.ecole or '').strip()
            if not ecole_name:
                continue
            if 'genie' in ecole_name.lower() and 'system' in ecole_name.lower():
                continue
            base = 0.0
            if getattr(ins, 'prix_total', None) is not None:
                base = float(ins.prix_total or 0)
            elif getattr(ins, 'formation', None) and getattr(ins.formation, 'prix_etudiant', None) is not None:
                base = float(ins.formation.prix_etudiant or 0)

            fname = (getattr(ins.formation, 'nom', '') or '').lower() if getattr(ins, 'formation', None) else ''
            if 'fibre' in fname:
                try:
                    if (ecole_name or '').strip().lower() == 'jura school':
                        deducted = 4500.0
                    else:
                        deducted = 3000.0
                except Exception:
                    deducted = 3000.0
            elif 'reseau' in fname or 'camera' in fname or 'caméra' in fname:
                deducted = 2000.0
            else:
                deducted = 0.0

            remaining = max(0.0, base - deducted)
            school_share = (remaining / 2.0) + deducted
            ecoles[ecole_name] += school_share
            ecoles_counts[ecole_name] += 1
        except Exception:
            continue

    # prepare JSON embed values for client-side filtering if needed
    try:
        import json as _json
        ecoles_summary = dict(ecoles)
        ecoles_counts = dict(ecoles_counts)
        # subtract payments already made for each school so the per-school
        # summary shows remaining due
        try:
            from django.db.models import Sum as _Sum
            for ename in list(ecoles_summary.keys()):
                try:
                    paid_paiements = Paiement.objects.filter(ecole__iexact=ename).aggregate(total=_Sum('montant'))['total'] or 0
                    try:
                        paid_school = SchoolVersement.objects.filter(ecole__iexact=ename).aggregate(total=_Sum('montant'))['total'] or 0
                    except Exception:
                        paid_school = 0
                    paid = (paid_paiements or 0) + (paid_school or 0)
                    ecoles_summary[ename] = float(max(0.0, ecoles_summary.get(ename, 0.0) - float(paid)))
                except Exception:
                    continue
        except Exception:
            pass

        ecoles_summary_json = _json.dumps(ecoles_summary)
        ecoles_counts_json = _json.dumps(ecoles_counts)
    except Exception:
        ecoles_summary_json = '{}'
        ecoles_counts_json = '{}'

    selected = request.GET.get('ecole')
    students = []
    if selected:
        sel_qs = Inscription.objects.select_related('formation', 'etudiant').filter(ecole__iexact=selected)
        for ins in sel_qs:
            try:
                base = 0.0
                if getattr(ins, 'prix_total', None) is not None:
                    base = float(ins.prix_total or 0)
                elif getattr(ins, 'formation', None) and getattr(ins.formation, 'prix_etudiant', None) is not None:
                    base = float(ins.formation.prix_etudiant or 0)
                fname = (getattr(ins.formation, 'nom', '') or '').lower() if getattr(ins, 'formation', None) else ''
                if 'fibre' in fname:
                    try:
                        # when viewing a selected school use the `selected` value
                        if (selected or '').strip().lower() == 'jura school':
                            deducted = 4500.0
                        else:
                            deducted = 3000.0
                    except Exception:
                        deducted = 3000.0
                elif 'reseau' in fname or 'camera' in fname or 'caméra' in fname:
                    deducted = 2000.0
                else:
                    deducted = 0.0
                remaining = max(0.0, base - deducted)
                share = (remaining / 2.0) + deducted
                students.append({
                    'inscription_id': ins.id,
                    'name': f"{getattr(ins.etudiant,'prenom','')} {getattr(ins.etudiant,'nom','')}",
                    'formation': getattr(ins.formation, 'nom', '') or (ins.formation_text or ''),
                    'base': round(base, 2),
                    'deducted': round(deducted, 2),
                    'remaining': round(remaining, 2),
                    'share': round(share, 2),
                })
            except Exception:
                continue

        # build versement history from SchoolVersement records (preferred) and
        # fall back to older Paiement-based batches if needed. We show most
        # recent 10 entries.
        try:
            versement_history = []
            # prefer SchoolVersement entries (single-row per batch)
            sv_qs = SchoolVersement.objects.filter(ecole__iexact=selected).order_by('-date_versement')[:10]
            for sv in sv_qs:
                versement_history.append({'batch_id': sv.batch_id, 'montant_total': float(sv.montant), 'paiements_count': 1, 'date_versement': sv.date_versement})
            # if none found, attempt to discover old-style batches built from Paiement.remarques
            if not versement_history:
                payments = Paiement.objects.filter(ecole__iexact=selected).exclude(remarques__isnull=True).filter(remarques__icontains='VERSEMENT_BATCH:').order_by('-date_paiement')
                batches = {}
                for p in payments:
                    try:
                        m = re.search(r'VERSEMENT_BATCH:([A-Za-z0-9_-]+)', p.remarques or '')
                        if not m:
                            continue
                        bid = m.group(1)
                        if bid not in batches:
                            batches[bid] = {'total': Decimal('0.00'), 'count': 0, 'date': p.date_paiement}
                        batches[bid]['total'] += (p.montant or Decimal('0.00'))
                        batches[bid]['count'] += 1
                        if p.date_paiement and p.date_paiement > batches[bid]['date']:
                            batches[bid]['date'] = p.date_paiement
                    except Exception:
                        continue
                for bid, info in sorted(batches.items(), key=lambda x: x[1]['date'], reverse=True)[:10]:
                    versement_history.append({'batch_id': bid, 'montant_total': float(info['total']), 'paiements_count': info['count'], 'date_versement': info['date']})
        except Exception:
            versement_history = []
    else:
        versement_history = []

    context = {
        # expose the adjusted summary (after subtracting payments) when available
        'ecoles_summary': (ecoles_summary if 'ecoles_summary' in locals() else dict(ecoles)),
        'ecoles_counts': (ecoles_counts if 'ecoles_counts' in locals() else dict(ecoles_counts)),
        'ecoles_summary_json': ecoles_summary_json,
        'ecoles_counts_json': ecoles_counts_json,
        'selected_ecole': selected,
        'students': students,
        'versement_history': versement_history,
    }
    return render(request, 'ecole_paiements.html', context)


def ecole_paiements_export_pdf(request):
    """Export the list of students for a given school with fee breakdown (PDF or CSV fallback)."""
    selected = request.GET.get('ecole')
    if not selected:
        return HttpResponse('Missing ecole parameter', status=400)

    try:
        qs = Inscription.objects.select_related('formation','etudiant').filter(ecole__iexact=selected)
    except Exception:
        qs = Inscription.objects.none()

    # build rows
    rows = []
    headers = ['ID','Étudiant','Formation','Base (DA)','Consommables (DA)','Après déduction (DA)','Part école (DA)']
    for ins in qs:
        try:
            base = 0.0
            if getattr(ins, 'prix_total', None) is not None:
                base = float(ins.prix_total or 0)
            elif getattr(ins, 'formation', None) and getattr(ins.formation, 'prix_etudiant', None) is not None:
                base = float(ins.formation.prix_etudiant or 0)
            fname = (getattr(ins.formation, 'nom', '') or '').lower() if getattr(ins, 'formation', None) else ''
            if 'fibre' in fname:
                try:
                    if (selected or '').strip().lower() == 'jura school':
                        deducted = 4500.0
                    else:
                        deducted = 3000.0
                except Exception:
                    deducted = 3000.0
            elif 'reseau' in fname or 'camera' in fname or 'caméra' in fname:
                deducted = 2000.0
            else:
                deducted = 0.0
            remaining = max(0.0, base - deducted)
            share = (remaining / 2.0) + deducted
            student_name = f"{getattr(ins.etudiant,'prenom','')} {getattr(ins.etudiant,'nom','')}".strip()
            formation = getattr(ins.formation, 'nom', '') or (ins.formation_text or '')
            rows.append([str(ins.id), student_name, formation, f"{base:.2f}", f"{deducted:.2f}", f"{remaining:.2f}", f"{share:.2f}"])
        except Exception:
            continue

    # Try to use reportlab to generate a well-formatted PDF, otherwise fallback to CSV
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak
        from reportlab.lib import colors
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.enums import TA_RIGHT, TA_CENTER
    except Exception:
        # CSV fallback
        import csv
        from io import StringIO
        sio = StringIO()
        writer = csv.writer(sio)
        writer.writerow([h for h in headers])
        for r in rows:
            writer.writerow(r)
        resp = HttpResponse(sio.getvalue().encode('utf-8'), content_type='text/csv')
        safe_name = (selected or 'ecole').replace(' ', '_')
        resp['Content-Disposition'] = f'attachment; filename=ecole_{safe_name}_students.csv'
        return resp

    # helper to format numbers with space as thousand separator and 2 decimals
    def fmt_num(v):
        try:
            return ("{:,.2f}".format(float(v))).replace(',', ' ')
        except Exception:
            return str(v)

    # compute totals for numeric columns (base, deducted, remaining, share)
    tot_base = sum([float(r[3].replace(' ', '').replace(',', '')) for r in rows]) if rows else 0.0
    tot_deducted = sum([float(r[4].replace(' ', '').replace(',', '')) for r in rows]) if rows else 0.0
    tot_remaining = sum([float(r[5].replace(' ', '').replace(',', '')) for r in rows]) if rows else 0.0
    tot_share = sum([float(r[6].replace(' ', '').replace(',', '')) for r in rows]) if rows else 0.0

    # format data strings for PDF
    pdf_rows = []
    for r in rows:
        pdf_rows.append([r[0], r[1], r[2], fmt_num(r[3]), fmt_num(r[4]), fmt_num(r[5]), fmt_num(r[6])])

    total_row = ['', 'TOTAL', '', fmt_num(tot_base), fmt_num(tot_deducted), fmt_num(tot_remaining), fmt_num(tot_share)]

    from io import BytesIO
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=24, rightMargin=24, topMargin=36, bottomMargin=24)
    styles = getSampleStyleSheet()
    # small styles
    header_style = ParagraphStyle('header', parent=styles['Heading2'], alignment=TA_CENTER)
    small = ParagraphStyle('small', parent=styles['Normal'], fontSize=9)
    elems = []
    elems.append(Paragraph(f'École: {selected}', header_style))
    elems.append(Spacer(1, 6))
    elems.append(Paragraph('Liste des étudiants et détails des frais (base, consommables, part école)', small))
    elems.append(Spacer(1, 12))

    data = [headers] + pdf_rows + [total_row]

    # column widths (approx) - ensure total <= page width (A4 width 595.27 pts minus margins)
    colWidths = [40, 160, 120, 50, 50, 50, 50]

    table = Table(data, colWidths=colWidths, repeatRows=1, hAlign='LEFT')
    tbl_style = TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#EEF2FF')),
        ('TEXTCOLOR', (0,0), (-1,0), colors.HexColor('#1E293B')),
        ('GRID', (0,0), (-1,-1), 0.4, colors.HexColor('#CBD5E1')),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,0), 10),
        ('FONTSIZE', (0,1), (-1,-1), 9),
        ('ALIGN', (3,1), (-1,-2), 'RIGHT'),
        ('ALIGN', (3,-1), (-1,-1), 'RIGHT'),
        ('SPAN', (0,-1), (1,-1)),
        ('FONTNAME', (0,-1), (-1,-1), 'Helvetica-Bold'),
        ('BACKGROUND', (0,-1), (-1,-1), colors.HexColor('#F8FAFC')),
    ])
    table.setStyle(tbl_style)
    elems.append(table)
    doc.build(elems)
    buffer.seek(0)
    resp = HttpResponse(buffer.read(), content_type='application/pdf')
    safe_name = (selected or 'ecole').replace(' ', '_')
    resp['Content-Disposition'] = f'attachment; filename=ecole_{safe_name}_students.pdf'
    return resp


@require_POST
def add_ecole_paiements_bulk(request):
    """Accept JSON body { inscription_ids: [1,2,3] } and create Paiement rows
    for each selected inscription using the same school-share formula.
    """
    try:
        payload = {}
        try:
            payload = json.loads(request.body.decode('utf-8') or '{}')
        except Exception:
            # fallback to POST form data
            payload = request.POST.dict()
        ids = payload.get('inscription_ids') or []
        created = []
        for iid in ids:
            try:
                ins = Inscription.objects.select_related('etudiant','formation').get(pk=int(iid))
                base = 0.0
                if getattr(ins, 'prix_total', None) is not None:
                    base = float(ins.prix_total or 0)
                elif getattr(ins, 'formation', None) and getattr(ins.formation, 'prix_etudiant', None) is not None:
                    base = float(ins.formation.prix_etudiant or 0)
                fname = (getattr(ins.formation, 'nom', '') or '').lower() if getattr(ins, 'formation', None) else ''
                if 'fibre' in fname:
                    try:
                        if (ins.ecole or '').strip().lower() == 'jura school':
                            deducted = 4500.0
                        else:
                            deducted = 3000.0
                    except Exception:
                        deducted = 3000.0
                elif 'reseau' in fname or 'camera' in fname or 'caméra' in fname:
                    deducted = 2000.0
                else:
                    deducted = 0.0
                remaining = max(0.0, base - deducted)
                share = Decimal((remaining / 2.0) + deducted)
                # create paiement record (date_paiement uses today)
                p = Paiement.objects.create(
                    etudiant=ins.etudiant,
                    formation=ins.formation,
                    inscription=ins,
                    montant=share,
                    date_paiement=timezone.now().date(),
                    ecole=ins.ecole or ''
                )
                created.append(p.pk)
            except Exception:
                continue
        return JsonResponse({'success': True, 'created': created})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@require_POST
def add_ecole_versement(request):
    """Apply a total versement for an external school and distribute it across inscriptions.

    Expects JSON body: { ecole: 'Name', total_montant: 12345.67, note: 'optional note' }

    The endpoint will:
      - find all Inscription rows with matching ecole
      - compute each inscription's fee (prix_total or formation.prix_etudiant) and distribute the
        provided total_montant proportionally to those fees (equal split if fees are not available)
      - create one Paiement per inscription with montant set to the computed portion
      - mark each Paiement.remarques with a batch marker 'VERSEMENT_BATCH:<batch_id>' so we can
        later group payments into versement history without adding a new table.
    """
    try:
        payload = {}
        try:
            payload = json.loads(request.body.decode('utf-8') or '{}')
        except Exception:
            payload = request.POST.dict()
        ecole = (payload.get('ecole') or '').strip()
        total_montant = payload.get('total_montant') or payload.get('montant')
        note = payload.get('note') or ''
        if not ecole or not total_montant:
            return JsonResponse({'success': False, 'error': 'ecole and total_montant required'}, status=400)
        total_montant = Decimal(str(total_montant))

        # generate a short batch id and marker
        import uuid as _uuid
        batch_id = f"V{_uuid.uuid4().hex[:8]}"
        marker = f"VERSEMENT_BATCH:{batch_id}"

        # Instead of creating a Paiement per inscription, record a single
        # SchoolVersement object representing the total versement for this
        # external school. This avoids creating individual student payments
        # while still allowing the UI to deduct the paid amount from the
        # school's remaining fees.
        try:
            sv = SchoolVersement.objects.create(
                ecole=ecole,
                montant=total_montant,
                date_versement=timezone.now().date(),
                batch_id=batch_id,
                note=note
            )
            created = [sv.pk]
            return JsonResponse({'success': True, 'created': created, 'batch_id': batch_id})
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)}, status=500)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


def ecole_versement_detail(request):
    """Return JSON list of Paiement rows that belong to a given versement batch_id.

    Query param: batch_id=Vxxxxxxxx
    """
    try:
        batch_id = (request.GET.get('batch_id') or request.GET.get('bid') or '').strip()
        if not batch_id:
            return JsonResponse({'success': False, 'error': 'batch_id required'}, status=400)
        marker = f"VERSEMENT_BATCH:{batch_id}"
        # first try SchoolVersement (preferred)
        try:
            sv = SchoolVersement.objects.filter(batch_id=batch_id).first()
            if sv:
                out = [{
                    'id': sv.id,
                    'reference': sv.batch_id,
                    'etudiant': '',
                    'montant': str(sv.montant),
                    'date_paiement': sv.date_versement.isoformat() if sv.date_versement else None,
                    'remarques': sv.note or '',
                    'ecole': sv.ecole,
                }]
                return JsonResponse({'success': True, 'paiements': out})
        except Exception:
            pass

        qs = Paiement.objects.filter(remarques__icontains=marker).select_related('etudiant', 'formation').order_by('-date_paiement')
        out = []
        for p in qs:
            out.append({
                'id': p.id,
                'reference': p.reference,
                'etudiant': f"{getattr(p.etudiant,'prenom','') or ''} {getattr(p.etudiant,'nom','') or ''}",
                'montant': str(p.montant),
                'date_paiement': p.date_paiement.isoformat() if p.date_paiement else None,
                'remarques': p.remarques,
                'ecole': p.ecole,
            })
        return JsonResponse({'success': True, 'paiements': out})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@require_POST
def delete_paiement(request):
    """Delete a single Paiement by id. Accepts JSON body or form data: { id: 123 }.

    After deletion we recompute balances for the affected student (best-effort).
    """
    try:
        payload = {}
        try:
            payload = json.loads(request.body.decode('utf-8') or '{}')
        except Exception:
            payload = request.POST.dict()
        pid = payload.get('id') or payload.get('_id')
        if not pid:
            return JsonResponse({'success': False, 'error': 'id required'}, status=400)
        try:
            pid = int(pid)
        except Exception:
            return JsonResponse({'success': False, 'error': 'invalid id'}, status=400)
        p = Paiement.objects.filter(pk=pid).first()
        if not p:
            return JsonResponse({'success': False, 'error': 'Paiement introuvable'}, status=404)
        etu_id = p.etudiant.id if getattr(p, 'etudiant', None) else None
        # only allow deleting school versements or payments that have an ecole set
        if not (p.ecole or (p.remarques and 'VERSEMENT_BATCH:' in p.remarques)):
            # still allow deletion if user has explicit permission via admin; here we return forbidden
            return JsonResponse({'success': False, 'error': 'Suppression limitée aux versements d\'école'}, status=403)
        p.delete()

        # recompute balances for student
        try:
            if etu_id:
                qs = Paiement.objects.filter(etudiant_id=etu_id).order_by('date_paiement', 'id')
                for pay in qs:
                    try:
                        res = calculate_balances(etu_id, payment_date=pay.date_paiement, montant=pay.montant, exclude_payment_pk=pay.pk)
                        apres = res.get('apres_solde')
                        statut = 'Réglé' if (apres == 0 or apres == 0.0) else 'Non réglé'
                        Paiement.objects.filter(pk=pay.pk).update(balance_after=apres, statut=statut)
                    except Exception:
                        continue
        except Exception:
            pass

        return JsonResponse({'success': True, 'deleted': pid})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@require_POST
def delete_versement_batch(request):
    """Delete all Paiement rows belonging to a versement batch (marker in remarques).

    Body: { batch_id: 'Vxxxxxx' }
    """
    try:
        payload = {}
        try:
            payload = json.loads(request.body.decode('utf-8') or '{}')
        except Exception:
            payload = request.POST.dict()
        batch_id = (payload.get('batch_id') or payload.get('bid') or '').strip()
        if not batch_id:
            return JsonResponse({'success': False, 'error': 'batch_id required'}, status=400)
        marker = f"VERSEMENT_BATCH:{batch_id}"
        deleted_count = 0

        # delete SchoolVersement(s) if present
        try:
            svs = list(SchoolVersement.objects.filter(batch_id=batch_id))
            if svs:
                deleted_count += len(svs)
                SchoolVersement.objects.filter(batch_id=batch_id).delete()
        except Exception:
            pass

        # then delete any legacy Paiement rows belonging to the batch
        qs = list(Paiement.objects.filter(remarques__icontains=marker))
        if qs:
            etu_ids = set(p.etudiant.id for p in qs if getattr(p, 'etudiant', None))
            count = len(qs)
            deleted_count += count
            Paiement.objects.filter(remarques__icontains=marker).delete()

            # recompute balances for affected students
            try:
                for etu_id in etu_ids:
                    for pay in Paiement.objects.filter(etudiant_id=etu_id).order_by('date_paiement', 'id'):
                        try:
                            res = calculate_balances(etu_id, payment_date=pay.date_paiement, montant=pay.montant, exclude_payment_pk=pay.pk)
                            apres = res.get('apres_solde')
                            statut = 'Réglé' if (apres == 0 or apres == 0.0) else 'Non réglé'
                            Paiement.objects.filter(pk=pay.pk).update(balance_after=apres, statut=statut)
                        except Exception:
                            continue
            except Exception:
                pass

        if deleted_count == 0:
            return JsonResponse({'success': True, 'deleted': 0, 'message': 'Aucun paiement ou versement trouvé pour ce batch'})

        return JsonResponse({'success': True, 'deleted': deleted_count})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


### ges CRUD
from django import forms
from .models import Charge
import os
import uuid
from django.conf import settings
from .ai_utils import extract_text_from_file, parse_text_with_openrouter, parse_text_auto, parse_chat_auto, parse_chat_with_grok, parse_chat_with_grok_structured
import traceback


def api_invest_totals(request):
    """Return JSON total of investment outflows (ReglementEnseignants + Charge).
    Query params:
      - period: 'day'|'month'|'year' (default 'day')
      - date: optional YYYY-MM-DD used when period=day to specify the day
    """
    from django.http import JsonResponse
    try:
        period = request.GET.get('period', 'day')
        d = request.GET.get('date')
        from django.utils import timezone as dj_timezone
        now_dt = dj_timezone.now()
        if period == 'day':
            if d:
                # parse provided date
                try:
                    dt = dj_timezone.datetime.fromisoformat(d)
                except Exception:
                    # fallback: try date() parse
                    try:
                        dt = dj_timezone.datetime.strptime(d, '%Y-%m-%d')
                    except Exception:
                        dt = now_dt
            else:
                dt = now_dt
            day_start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
            day_end = day_start + dj_timezone.timedelta(days=1)
            r_sum = ReglementEnseignants.objects.filter(date_reglement__gte=day_start.date(), date_reglement__lt=day_end.date()).aggregate(total=Sum('montant'))['total'] or 0
            c_sum = Charge.objects.filter(date_paiement__gte=day_start.date(), date_paiement__lt=day_end.date()).aggregate(total=Sum('montant'))['total'] or 0
            total = (r_sum or 0) + (c_sum or 0)
            return JsonResponse({'success': True, 'total': total})
        elif period == 'month':
            month_start = now_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            r_sum = ReglementEnseignants.objects.filter(date_reglement__gte=month_start.date()).aggregate(total=Sum('montant'))['total'] or 0
            c_sum = Charge.objects.filter(date_paiement__gte=month_start.date()).aggregate(total=Sum('montant'))['total'] or 0
            total = (r_sum or 0) + (c_sum or 0)
            return JsonResponse({'success': True, 'total': total})
        elif period == 'year':
            year_start = now_dt.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            r_sum = ReglementEnseignants.objects.filter(date_reglement__gte=year_start.date()).aggregate(total=Sum('montant'))['total'] or 0
            c_sum = Charge.objects.filter(date_paiement__gte=year_start.date()).aggregate(total=Sum('montant'))['total'] or 0
            total = (r_sum or 0) + (c_sum or 0)
            return JsonResponse({'success': True, 'total': total})
        else:
            return JsonResponse({'success': False, 'error': 'Invalid period'}, status=400)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


class ChargeForm(forms.ModelForm):
    # separate UI fields: a main "type" and a "sous-type". These are not stored as
    # separate DB columns but combined into Charge.type_charge on save to preserve
    # existing schema.
    # Updated choices: main types are 'fonctionnement' and 'administratif'.
    # Sub-types follow the requested mapping.
    type_main = forms.ChoiceField(
        choices=[
            ('travaux', 'Travaux et entretien'),
            ('amenagement', 'Aménagement'),
            ('vehicule', 'Dépenses Véhicule'),
            ('fiscales', 'Charges Fiscales'),
            ('fonctionnement', 'Charge de fonctionnement'),
            ('autres', 'Autres frais'),
            ('abonnement', 'Charge abonnement'),
        ],
        required=False,
        widget=forms.Select(attrs={'class': 'form-control'})
    )
    # list all possible sous-types (frontend will filter them according to selected main type)
    sous_type = forms.ChoiceField(
        choices=[
            ('', '--- Choisir un sous-type ---'),
            # Travaux et entretien
            ('maconnerie', 'Maçonnerie'),
            ('plomberie', 'Plomberie'),
            ('carrelage', 'Carrelage et faïence'),
            ('peinture', 'Peinture'),
            ('courant_fort', 'Courant fort'),
            ('courant_faible', 'Courant faible'),
            ('menuiserie', 'Menuiserie générale'),
            ('finitions', 'Finitions'),
            ('soudeur', 'Soudeur'),
            ('quincaillerie', 'Quincaillerie'),
            # Aménagement
            ('mobilier', 'Mobilier'),
            ('materiel_equipement', 'Matériel et équipement'),
            # Véhicule
            ('carburant', 'Carburant'),
            ('assurance', 'Assurance'),
            ('entretien_mecanique', 'Entretien et mécanique'),
            # Fiscales
            ('impot', 'Impôt'),
            ('cnas', 'CNAS'),
            ('casnos', 'CASNOS'),
            ('quittance_fiscale', 'Quittance fiscale'),
            # Fonctionnement
            ('fourniture_bureautique', 'Fourniture bureautique'),
            ('impression_photocopie', 'Impression et photocopie'),
            ('publicite', 'Publicité'),
            ('loyer', 'LOYER'),
            ('entretien_nettoyage', 'Entretien et nettoyage'),
            # Autres frais
            ('alimentation', 'Alimentations'),
            ('cafe_restaurant', 'Café & Restaurant'),
            ('voyage', 'Voyage'),
            ('transport', 'Transport'),
            ('divers', 'Divers'),
            # Abonnement / factures
            ('facture_electricite', 'facture d\'électricité'),
            ('facture_gaz', 'facture Gaz'),
            ('facture_internet', 'facture internet'),
            ('facture_telephone', 'facture téléphone'),
            ('facture_TV', 'facture TV'),
        ],
        required=False,
        widget=forms.Select(attrs={'class': 'form-control'})
    )
    # optional attachment (image/pdf) to auto-fill the description
    attachment = forms.FileField(required=False, widget=forms.ClearableFileInput(attrs={'class': 'form-control'}))

    # UI sub-fields for contact: stored together in the model field `contact`.
    contact_name = forms.CharField(required=False, widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Nom du contact'}))
    contact_phone = forms.CharField(required=False, widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Téléphone'}))

    class Meta:
        model = Charge
        # Expose montant/reference/remarque in the model form.
        # Contact is captured via the UI sub-fields contact_name/contact_phone
        # and combined into the single model field `contact` on save.
        fields = ['montant', 'reference', 'remarque']
        widgets = {
            'remarque': forms.Textarea(attrs={'rows': 3, 'class': 'form-control'}),
            'montant': forms.NumberInput(attrs={'class': 'form-control'}),
            'reference': forms.TextInput(attrs={'class': 'form-control'}),
        }

    def __init__(self, *args, **kwargs):
        # if editing an existing instance, try to prefill the UI type fields from
        # instance.type_charge. We try a best-effort parse (separate on ' : ' or
        # ' - ' or simply leave whole value as type_main).
        instance = kwargs.get('instance')
        super().__init__(*args, **kwargs)
        if instance and getattr(instance, 'type_charge', None):
            tc = instance.type_charge or ''
            # Attempt to split into main / sous if a separator is present
            if ' : ' in tc:
                left, right = tc.split(' : ', 1)
                self.fields['type_main'].initial = left
                self.fields['sous_type'].initial = right
            elif ' - ' in tc:
                left, right = tc.split(' - ', 1)
                self.fields['type_main'].initial = left
                self.fields['sous_type'].initial = right
            else:
                # no clear separator; put the whole value in type_main so the
                # admin can adjust it
                self.fields['type_main'].initial = tc

            # Initialize contact sub-fields from model field `contact` if present.
            try:
                contact_val = (instance.contact or '') if instance else ''
                if contact_val:
                    # stored format: 'name||phone' (uses '||' as separator)
                    parts = contact_val.split('||')
                    name = parts[0] if len(parts) > 0 else ''
                    phone = parts[1] if len(parts) > 1 else ''
                    self.fields['contact_name'].initial = name
                    self.fields['contact_phone'].initial = phone
            except Exception:
                pass

    def save(self, commit=True):
        # combine UI fields into the model field type_charge
        instance = super().save(commit=False)
        # Combine contact_name/contact_phone into instance.contact using '||' separator
        try:
            name = (self.cleaned_data.get('contact_name') or '').strip()
            phone = (self.cleaned_data.get('contact_phone') or '').strip()
            if name or phone:
                instance.contact = f"{name}||{phone}"
            else:
                instance.contact = None
        except Exception:
            # ignore and continue
            pass
        t_main = self.cleaned_data.get('type_main') or ''
        s_type = self.cleaned_data.get('sous_type') or ''
        # choose representation: prefer readable labels for sous_type when set
        # find label for selected sous_type
        sous_label = ''
        if s_type:
            for k, lab in self.fields['sous_type'].choices:
                if k == s_type:
                    sous_label = lab
                    break
        main_label = ''
        if t_main:
            for k, lab in self.fields['type_main'].choices:
                if k == t_main:
                    main_label = lab
                    break

        if main_label and sous_label:
            instance.type_charge = f"{main_label} : {sous_label}"
        elif sous_label:
            instance.type_charge = sous_label
        elif main_label:
            instance.type_charge = main_label
        else:
            # fallback to existing value or empty
            instance.type_charge = getattr(instance, 'type_charge', '') or ''

        if commit:
            instance.save()
        return instance

    def clean(self):
        """Validate that the selected sous_type is compatible with type_main.

        Enforce exclusivity according to the project's mapping:
        - 'fiscale' -> CNAS, CASNOS, Impos
        - 'fonctionnement' -> Loyer, Gaz/Électricité, EAU, Internet, Téléphone, Travaux et entretien
        - 'administratif' -> Papier, Marqueurs, Dossier
        - 'marketing' -> Sponsoring, Cartes
        """
        cleaned = super().clean()
        type_main = cleaned.get('type_main')
        sous_type = cleaned.get('sous_type')

        TYPE_SUBTYPE_MAP = {
            'travaux': ['maconnerie', 'plomberie', 'carrelage', 'peinture', 'courant_fort', 'courant_faible', 'menuiserie', 'finitions', 'soudeur', 'quincaillerie'],
            'amenagement': ['mobilier', 'materiel_equipement'],
            'vehicule': ['carburant', 'assurance', 'entretien_mecanique'],
            'fiscales': ['impot', 'cnas', 'casnos', 'quittance_fiscale'],
            # Add 'loyer' here so it is accepted as a sous-type of 'fonctionnement'
            'fonctionnement': ['fourniture_bureautique', 'impression_photocopie', 'publicite', 'loyer', 'entretien_nettoyage'],
            'autres': ['alimentation', 'cafe_restaurant', 'voyage', 'transport', 'divers'],
            'abonnement': ['facture_electricite', 'facture_gaz', 'facture_internet', 'facture_telephone', 'facture_TV'],
        }

        # If a sous_type was selected, ensure a main type was also selected
        if sous_type:
            if not type_main:
                raise forms.ValidationError('Veuillez sélectionner le type principal correspondant au sous-type.')
            allowed = TYPE_SUBTYPE_MAP.get(type_main, [])
            if sous_type not in allowed:
                raise forms.ValidationError('Le sous-type sélectionné n\'est pas valide pour le type choisi.')

        return cleaned


def charges_list(request):
    qs = Charge.objects.all().order_by('-date_paiement')
    return render(request, 'charges/list.html', {'charges': qs})


def charge_create(request):
    if request.method == 'POST':
        form = ChargeForm(request.POST, request.FILES)
        if form.is_valid():
            obj = form.save(commit=False)
            # auto-set today's date if not provided
            if not getattr(obj, 'date_paiement', None):
                from datetime import date
                obj.date_paiement = date.today()

            # if an attachment was uploaded, save it and try to extract+parse
            attachment = request.FILES.get('attachment')
            if attachment:
                try:
                    media_root = getattr(settings, 'MEDIA_ROOT', None) or os.path.join(getattr(settings, 'BASE_DIR', ''), 'media')
                    attach_dir = os.path.join(media_root, 'charges_attachments')
                    os.makedirs(attach_dir, exist_ok=True)
                    safe_name = f"{uuid.uuid4().hex}_{attachment.name}"
                    dest_path = os.path.join(attach_dir, safe_name)
                    with open(dest_path, 'wb') as dst:
                        for chunk in attachment.chunks():
                            dst.write(chunk)
                    # write a small processing record (start)
                    try:
                        with open(os.path.join(attach_dir, 'process.log'), 'a', encoding='utf-8') as lf:
                            lf.write(f"[CREATE] Saved attachment {safe_name} (original: {attachment.name})\n")
                    except Exception:
                        pass

                    # try to extract text from the file
                    ocr_text, ocr_err = extract_text_from_file(dest_path)
                    try:
                        with open(os.path.join(attach_dir, 'process.log'), 'a', encoding='utf-8') as lf:
                            lf.write(f"[CREATE] OCR len={len(ocr_text or '')} err={ocr_err}\n")
                    except Exception:
                        pass
                    # call OpenRouter to produce a short description
                    parsed_desc = ''
                    if ocr_text:
                        parsed_desc = parse_text_auto(ocr_text)
                    try:
                        with open(os.path.join(attach_dir, 'process.log'), 'a', encoding='utf-8') as lf:
                            lf.write(f"[CREATE] Parsed desc len={len(parsed_desc or '')}\n")
                            if parsed_desc:
                                lf.write(parsed_desc.replace('\n',' ') + '\n')
                    except Exception:
                        pass
                    # if we got something, set it to remarque (description)
                    if parsed_desc:
                        obj.remarque = parsed_desc
                except Exception as e:
                    # best-effort only; don't fail the save on AI errors
                    try:
                        log_path = os.path.join(attach_dir, 'process.log')
                        with open(log_path, 'a', encoding='utf-8') as lf:
                            lf.write(f"[CREATE] Error processing {attachment.name}: {str(e)}\n")
                            lf.write(traceback.format_exc())
                            lf.write('\n')
                    except Exception:
                        pass

            obj.save()
            return redirect('charges')
    else:
        # allow pre-filling from AI modal via GET params: ai_remarque, ai_montant, ai_reference, ai_type
        ai_remarque = request.GET.get('ai_remarque', '')
        ai_montant = request.GET.get('ai_montant', '')
        ai_reference = request.GET.get('ai_reference', '')
        ai_type = request.GET.get('ai_type', '')
        initial = {}
        if ai_remarque:
            initial['remarque'] = ai_remarque
        if ai_montant:
            try:
                initial['montant'] = float(ai_montant)
            except Exception:
                initial['montant'] = ai_montant
        if ai_reference:
            initial['reference'] = ai_reference
        form = ChargeForm(initial=initial)
        # If ai_type looks like a known sous_type label or key, try to set it via JS in template (form init handles only instance parsing).
    return render(request, 'charges/form.html', {'form': form, 'creating': True})


def charge_edit(request, pk):
    obj = get_object_or_404(Charge, pk=pk)
    if request.method == 'POST':
        form = ChargeForm(request.POST, request.FILES, instance=obj)
        if form.is_valid():
            obj = form.save(commit=False)
            if not getattr(obj, 'date_paiement', None):
                from datetime import date
                obj.date_paiement = date.today()

            # process new attachment if provided
            attachment = request.FILES.get('attachment')
            if attachment:
                try:
                    media_root = getattr(settings, 'MEDIA_ROOT', None) or os.path.join(getattr(settings, 'BASE_DIR', ''), 'media')
                    attach_dir = os.path.join(media_root, 'charges_attachments')
                    os.makedirs(attach_dir, exist_ok=True)
                    safe_name = f"{uuid.uuid4().hex}_{attachment.name}"
                    dest_path = os.path.join(attach_dir, safe_name)
                    with open(dest_path, 'wb') as dst:
                        for chunk in attachment.chunks():
                            dst.write(chunk)
                    ocr_text, ocr_err = extract_text_from_file(dest_path)
                    if ocr_text:
                        parsed_desc = parse_text_auto(ocr_text)
                        if parsed_desc:
                            obj.remarque = parsed_desc
                except Exception:
                    pass

            obj.save()
            return redirect('charges')
    else:
        form = ChargeForm(instance=obj)
    return render(request, 'charges/form.html', {'form': form, 'creating': False, 'charge': obj})


def charge_delete(request, pk):
    obj = get_object_or_404(Charge, pk=pk)
    if request.method == 'POST':
        obj.delete()
        return redirect('charges')
    return render(request, 'charges/confirm_delete.html', {'charge': obj})


@require_POST
def charge_parse_ai(request):
    """AJAX endpoint: accept a file upload (image/pdf), run OCR + OpenRouter, return short description.

    Expects multipart POST with field 'file'. Returns JSON { success: True, description: '...' }
    """
    try:
        f = request.FILES.get('file')
        if not f:
            return JsonResponse({'success': False, 'error': 'No file provided'}, status=400)

        media_root = getattr(settings, 'MEDIA_ROOT', None) or os.path.join(getattr(settings, 'BASE_DIR', ''), 'media')
        attach_dir = os.path.join(media_root, 'charges_attachments')
        os.makedirs(attach_dir, exist_ok=True)
        safe_name = f"tmp_{uuid.uuid4().hex}_{f.name}"
        dest_path = os.path.join(attach_dir, safe_name)
        with open(dest_path, 'wb') as dst:
            for chunk in f.chunks():
                dst.write(chunk)

        # try OCR
        ocr_text, ocr_err = extract_text_from_file(dest_path)
        # log
        try:
            with open(os.path.join(attach_dir, 'process.log'), 'a', encoding='utf-8') as lf:
                lf.write(f"[PARSE] Saved {safe_name} OCR_len={len(ocr_text or '')} ocr_err={ocr_err}\n")
        except Exception:
            pass

        parsed_desc = ''
        if ocr_text:
            parsed_desc = parse_text_auto(ocr_text)

        try:
            with open(os.path.join(attach_dir, 'process.log'), 'a', encoding='utf-8') as lf:
                lf.write(f"[PARSE] parsed_len={len(parsed_desc or '')}\n")
                if parsed_desc:
                    lf.write(parsed_desc.replace('\n',' ') + '\n')
        except Exception:
            pass

        return JsonResponse({'success': True, 'description': parsed_desc or ''})
    except Exception as e:
        try:
            with open(os.path.join(getattr(settings, 'MEDIA_ROOT', '.'), 'charges_attachments', 'process.log'), 'a', encoding='utf-8') as lf:
                lf.write(f"[PARSE-ERR] {str(e)}\n")
                lf.write(traceback.format_exc())
        except Exception:
            pass
        return JsonResponse({'success': False, 'error': str(e)}, status=500)

@require_POST
def charge_ai_chat(request):
        """AJAX chat endpoint: accepts 'message' and optional file upload. Returns { success: True, reply: '...' }"""
        try:
            msg = request.POST.get('message', '') or request.POST.get('prompt', '')
            files = request.FILES.getlist('file') or []

            context_summary = None
            saved_files = []
            ocr_texts = []
            if files:
                media_root = getattr(settings, 'MEDIA_ROOT', None) or os.path.join(getattr(settings, 'BASE_DIR', ''), 'media')
                attach_dir = os.path.join(media_root, 'charges_attachments')
                os.makedirs(attach_dir, exist_ok=True)
                # Prefer using the Grok model on the raw images (base64) instead of OCR, per user request.
                images_for_model = []
                for f in files:
                    safe_name = f"tmp_{uuid.uuid4().hex}_{f.name}"
                    dest_path = os.path.join(attach_dir, safe_name)
                    with open(dest_path, 'wb') as dst:
                        for chunk in f.chunks():
                            dst.write(chunk)
                    saved_files.append({'original': f.name, 'saved': safe_name})
                    # create base64 data URL for images (for Grok to analyze directly)
                    try:
                        # only include image files as base64; for PDFs we still keep saved file but don't base64-embed
                        ct = getattr(f, 'content_type', '') or ''
                        if ct.startswith('image'):
                            with open(dest_path, 'rb') as rf:
                                b = rf.read()
                            b64 = base64.b64encode(b).decode('ascii')
                            data_url = f'data:{ct};base64,' + b64
                            images_for_model.append({'name': f.name, 'data_url': data_url})
                            try:
                                with open(os.path.join(attach_dir, 'process.log'), 'a', encoding='utf-8') as lf:
                                    lf.write(f"[CHAT] Saved {safe_name} image for model (len={len(b64)})\n")
                            except Exception:
                                pass
                        else:
                            # Non-image (pdf etc) - try OCR as fallback
                            ocr_text, ocr_err = extract_text_from_file(dest_path)
                            try:
                                with open(os.path.join(attach_dir, 'process.log'), 'a', encoding='utf-8') as lf:
                                    lf.write(f"[CHAT] Saved {safe_name} OCR_len={len(ocr_text or '')} ocr_err={ocr_err}\n")
                            except Exception:
                                pass
                            if ocr_text:
                                ocr_texts.append(ocr_text)
                    except Exception as e:
                        try:
                            with open(os.path.join(attach_dir, 'process.log'), 'a', encoding='utf-8') as lf:
                                lf.write(f"[CHAT-ERR] saving/encoding file {f.name}: {str(e)}\n")
                        except Exception:
                            pass
                # combine OCR texts for context
                combined = ''
                if ocr_texts:
                    combined = "\n\n".join(ocr_texts)
                    # keep this as fallback
                    context_summary = parse_text_auto(combined)
                else:
                    context_summary = None
                # If we have images_for_model, prefer calling Grok with images instead of relying on OCR
                if 'images_for_model' in locals() and images_for_model:
                    # call Grok structured and text analyzers using images
                    reply = parse_chat_with_grok(msg or '', context_text=None, images=images_for_model)
                    structured = parse_chat_with_grok_structured(msg or '', context_text=None, images=images_for_model)
                    # skip later OCR-based structured attempt
                    combined = combined
                    try:
                        with open(os.path.join(attach_dir, 'process.log'), 'a', encoding='utf-8') as lf:
                            lf.write(f"[CHAT] Called Grok with {len(images_for_model)} images_for_model\n")
                    except Exception:
                        pass
                    # skip the normal non-image path
                    if not reply or not str(reply).strip():
                        try:
                            with open(os.path.join(getattr(settings, 'MEDIA_ROOT', '.'), 'charges_attachments', 'process.log'), 'a', encoding='utf-8') as lf:
                                lf.write(f"[CHAT-ERR] model returned empty reply for msg={str(msg)[:200]} images_for_model={len(images_for_model)}\n")
                        except Exception:
                            pass
                        return JsonResponse({'success': False, 'error': 'Aucune réponse du modèle (vérifier la clé / quota / logs serveur).'})
                    try:
                        with open(os.path.join(getattr(settings, 'MEDIA_ROOT', '.'), 'charges_attachments', 'process.log'), 'a', encoding='utf-8') as lf:
                            lf.write(f"[CHAT] user={msg[:200]} images_for_model={len(images_for_model)} reply_len={len(reply or '')}\n")
                    except Exception:
                        pass
                    return JsonResponse({'success': True, 'reply': reply or '', 'structured': structured, 'ocr_text': (combined if ocr_texts else ''), 'files': saved_files})

            # Build the user message with optional context
            # Use the Grok-based helper (G intelligence) so the chat uses the requested model/key.
            reply = parse_chat_with_grok(msg or '', context_text=context_summary)
            structured = parse_chat_with_grok_structured(msg or '', context_text=context_summary)

            # If the model returned an empty reply, surface an error so the frontend shows a message
            if not reply or not str(reply).strip():
                try:
                    with open(os.path.join(getattr(settings, 'MEDIA_ROOT', '.'), 'charges_attachments', 'process.log'), 'a', encoding='utf-8') as lf:
                        lf.write(f"[CHAT-ERR] model returned empty reply for msg={str(msg)[:200]} context_len={(len(context_summary) if context_summary else 0)} saved_files={len(saved_files)}\n")
                except Exception:
                    pass
                return JsonResponse({'success': False, 'error': 'Aucune réponse du modèle (vérifier la clé / quota / logs serveur).'})
            try:
                with open(os.path.join(getattr(settings, 'MEDIA_ROOT', '.'), 'charges_attachments', 'process.log'), 'a', encoding='utf-8') as lf:
                    lf.write(f"[CHAT] user={msg[:200]} context_len={(len(context_summary) if context_summary else 0)} reply_len={len(reply or '')}\n")
            except Exception:
                pass

            return JsonResponse({'success': True, 'reply': reply or '', 'structured': structured, 'ocr_text': (combined if ocr_texts else ''), 'files': saved_files})
        except Exception as e:
            try:
                with open(os.path.join(getattr(settings, 'MEDIA_ROOT', '.'), 'charges_attachments', 'process.log'), 'a', encoding='utf-8') as lf:
                    lf.write(f"[CHAT-ERR] {str(e)}\n")
                    lf.write(traceback.format_exc())
            except Exception:
                pass
            return JsonResponse({'success': False, 'error': str(e)}, status=500)


# ============================================
# API REST Mobile pour Charges
# ============================================

@csrf_exempt
def api_charges_list(request):
    """API endpoint: GET all charges as JSON for mobile app.
    
    Returns JSON: { success: True, charges: [...], stats: {...} }
    Query params:
      - limit: max number of charges to return (default: 100)
      - offset: pagination offset
      - month: YYYY-MM to filter by month
    """
    try:
        limit = int(request.GET.get('limit', 100))
        offset = int(request.GET.get('offset', 0))
        month_filter = request.GET.get('month', '')
        
        qs = Charge.objects.all().order_by('-date_paiement')
        
        # Filter by month if provided
        if month_filter:
            try:
                year, month = month_filter.split('-')
                qs = qs.filter(date_paiement__year=int(year), date_paiement__month=int(month))
            except Exception:
                pass
        
        total_count = qs.count()
        charges = qs[offset:offset + limit]
        
        # Build response data
        charges_data = []
        for c in charges:
            attachment_url = None
            if getattr(c, 'attachment', None):
                attachment_url = request.build_absolute_uri(c.attachment.url)
            
            charges_data.append({
                'id': c.id,
                'type_charge': c.type_charge or '',
                'montant': float(c.montant or 0),
                'date_paiement': c.date_paiement.isoformat() if c.date_paiement else None,
                'mode_paiement': c.mode_paiement or '',
                'reference': c.reference or '',
                'remarque': c.remarque or '',
                'formation_id': c.formation_id,
                'formation_nom': c.formation.nom if c.formation else None,
                'contact': c.contact or '',
                'attachment': attachment_url,
            })
        
        # Compute stats
        now = timezone.now()
        total_all = Charge.objects.aggregate(total=Sum('montant'))['total'] or 0
        month_total = Charge.objects.filter(
            date_paiement__year=now.year,
            date_paiement__month=now.month
        ).aggregate(total=Sum('montant'))['total'] or 0
        
        # Type distribution for chart
        type_stats = Charge.objects.values('type_charge').annotate(
            total=Sum('montant'),
            count=Count('id')
        ).order_by('-total')[:10]
        
        return JsonResponse({
            'success': True,
            'charges': charges_data,
            'total_count': total_count,
            'stats': {
                'total_all': float(total_all),
                'month_total': float(month_total),
                'count': total_count,
                'type_distribution': list(type_stats),
            }
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@csrf_exempt
def api_charges_all(request):
    """API endpoint: GET all charges with pagination for a dedicated list view."""
    try:
        limit = int(request.GET.get('limit', 20))
        page = int(request.GET.get('page', 1))
        offset = (page - 1) * limit
        
        qs = Charge.objects.all().order_by('-date_paiement', '-id')
        total_count = qs.count()
        charges = qs[offset:offset + limit]
        
        charges_data = []
        for c in charges:
            attachment_url = None
            if getattr(c, 'attachment', None):
                attachment_url = request.build_absolute_uri(c.attachment.url)
                
            charges_data.append({
                'id': c.id,
                'type_charge': c.type_charge or '',
                'montant': float(c.montant or 0),
                'date_paiement': c.date_paiement.isoformat() if c.date_paiement else None,
                'mode_paiement': c.mode_paiement or '',
                'reference': c.reference or '',
                'remarque': c.remarque or '',
                'contact': c.contact or '',
                'fournisseur': c.fournisseur or '',
                'nom_contact': c.nom_contact or '',
                'attachment': attachment_url,
            })
            
        return JsonResponse({
            'success': True,
            'charges': charges_data,
            'total': total_count,
            'page': page,
            'pages': (total_count + limit - 1) // limit
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@csrf_exempt
def api_charge_detail(request, pk):
    """API endpoint: GET single charge by ID.
    
    Returns JSON: { success: True, charge: {...} }
    """
    try:
        c = get_object_or_404(Charge, pk=pk)
        attachment_url = None
        if getattr(c, 'attachment', None):
            attachment_url = request.build_absolute_uri(c.attachment.url)
            
        charge_data = {
            'id': c.id,
            'type_charge': c.type_charge or '',
            'montant': float(c.montant or 0),
            'date_paiement': c.date_paiement.isoformat() if c.date_paiement else None,
            'mode_paiement': c.mode_paiement or '',
            'reference': c.reference or '',
            'remarque': c.remarque or '',
            'formation_id': c.formation_id,
            'formation_nom': c.formation.nom if c.formation else None,
            'contact': c.contact or '',
            'fournisseur': c.fournisseur or '',
            'nom_contact': c.nom_contact or '',
            'attachment': attachment_url,
        }
        return JsonResponse({'success': True, 'charge': charge_data})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@csrf_exempt
@require_POST
def api_charge_create(request):
    """API endpoint: POST to create a new charge.
    
    Accepts JSON body or form-data:
      - type_charge: string (required)
      - montant: decimal (required)
      - date_paiement: YYYY-MM-DD (optional, defaults to today)
      - mode_paiement: string (optional)
      - reference: string (optional)
      - remarque: string (optional)
      - contact: string (optional)
      - formation_id: int (optional)
    
    Returns JSON: { success: True, charge: {...}, id: int }
    """
    try:
        # Parse request data
        if request.content_type and 'application/json' in request.content_type:
            try:
                data = json.loads(request.body.decode('utf-8') or '{}')
            except Exception:
                data = {}
        else:
            data = request.POST.dict()
        
        # Validate required fields
        type_charge = data.get('type_charge', '').strip()
        montant = data.get('montant')
        
        if not type_charge:
            return JsonResponse({'success': False, 'error': 'type_charge is required'}, status=400)
        if montant is None:
            return JsonResponse({'success': False, 'error': 'montant is required'}, status=400)
        
        try:
            montant = Decimal(str(montant))
        except Exception:
            return JsonResponse({'success': False, 'error': 'Invalid montant value'}, status=400)
        
        # Parse date
        date_paiement_str = data.get('date_paiement', '')
        if date_paiement_str:
            try:
                date_paiement = datetime.strptime(date_paiement_str, '%Y-%m-%d').date()
            except Exception:
                date_paiement = date.today()
        else:
            date_paiement = date.today()
        
        # Create charge
        charge = Charge.objects.create(
            type_charge=type_charge,
            montant=montant,
            date_paiement=date_paiement,
            mode_paiement=data.get('mode_paiement', '') or None,
            reference=data.get('reference', '') or None,
            remarque=data.get('remarque', '') or None,
            contact=data.get('contact', '') or None,
            fournisseur=data.get('fournisseur', '') or None,
            nom_contact=data.get('nom_contact', '') or None,
            formation_id=data.get('formation_id') if data.get('formation_id') else None,
        )

        # Handle attachment if any
        if request.FILES.get('attachment'):
            charge.attachment = request.FILES['attachment']
            charge.save()
            
        attachment_url = None
        if getattr(charge, 'attachment', None):
            attachment_url = charge.attachment.url

        charge_data = {
            'id': charge.id,
            'type_charge': charge.type_charge or '',
            'montant': float(charge.montant or 0),
            'date_paiement': charge.date_paiement.isoformat() if charge.date_paiement else None,
            'mode_paiement': charge.mode_paiement or '',
            'reference': charge.reference or '',
            'remarque': charge.remarque or '',
            'contact': charge.contact or '',
            'fournisseur': charge.fournisseur or '',
            'nom_contact': charge.nom_contact or '',
            'formation_id': charge.formation_id,
            'formation_nom': charge.formation.nom if charge.formation else None,
            'attachment': attachment_url,
        }
        
        return JsonResponse({'success': True, 'charge': charge_data})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@csrf_exempt
@require_POST
def api_charge_update(request, pk):
    """API endpoint: POST to update an existing charge.
    
    Accepts JSON body or form-data with same fields as create.
    Returns JSON: { success: True, charge: {...} }
    """
    try:
        charge = get_object_or_404(Charge, pk=pk)
        
        # Parse request data
        if request.content_type and 'application/json' in request.content_type:
            try:
                data = json.loads(request.body.decode('utf-8') or '{}')
            except Exception:
                data = {}
        else:
            data = request.POST.dict()
        
        # Update fields if provided
        if 'type_charge' in data and data['type_charge']:
            charge.type_charge = data['type_charge'].strip()
        
        if 'montant' in data:
            try:
                charge.montant = Decimal(str(data['montant']))
            except Exception:
                pass
        
        if 'date_paiement' in data and data['date_paiement']:
            try:
                charge.date_paiement = datetime.strptime(data['date_paiement'], '%Y-%m-%d').date()
            except Exception:
                pass
        
        if 'mode_paiement' in data:
            charge.mode_paiement = data['mode_paiement'] or None
        
        if 'reference' in data:
            charge.reference = data['reference'] or None
        
        if 'remarque' in data:
            charge.remarque = data['remarque'] or None
        
        if 'contact' in data:
            charge.contact = data['contact'] or None
        
        if 'fournisseur' in data:
            charge.fournisseur = data['fournisseur'] or None
        
        if 'nom_contact' in data:
            charge.nom_contact = data['nom_contact'] or None
        
        if 'formation_id' in data:
            charge.formation_id = data['formation_id'] if data['formation_id'] else None
        
        # Handle attachment from FILES if any
        if request.FILES.get('attachment'):
            charge.attachment = request.FILES['attachment']
        
        charge.save()
        
        attachment_url = None
        if getattr(charge, 'attachment', None):
            attachment_url = request.build_absolute_uri(charge.attachment.url)
        
        charge_data = {
            'id': charge.id,
            'type_charge': charge.type_charge or '',
            'montant': float(charge.montant or 0),
            'date_paiement': charge.date_paiement.isoformat() if charge.date_paiement else None,
            'mode_paiement': charge.mode_paiement or '',
            'reference': charge.reference or '',
            'remarque': charge.remarque or '',
            'formation_id': charge.formation_id,
            'formation_nom': charge.formation.nom if charge.formation else None,
            'contact': charge.contact or '',
            'fournisseur': charge.fournisseur or '',
            'nom_contact': charge.nom_contact or '',
            'attachment': attachment_url,
        }
        
        return JsonResponse({'success': True, 'charge': charge_data})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@csrf_exempt
@require_POST
def api_charge_delete(request, pk):
    """API endpoint: POST to delete a charge.
    
    Returns JSON: { success: True, deleted: int }
    """
    try:
        charge = get_object_or_404(Charge, pk=pk)
        charge_id = charge.id
        charge.delete()
        return JsonResponse({'success': True, 'deleted': charge_id})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@csrf_exempt
def api_charges_stats(request):
    """API endpoint: GET charges statistics for dashboard.
    
    Returns JSON: { success: True, stats: {...} }
    """
    try:
        now = timezone.now()
        
        # Total charges
        total_all = Charge.objects.aggregate(total=Sum('montant'))['total'] or 0
        total_count = Charge.objects.count()
        
        # This month
        month_charges = Charge.objects.filter(
            date_paiement__year=now.year,
            date_paiement__month=now.month
        )
        month_total = month_charges.aggregate(total=Sum('montant'))['total'] or 0
        month_count = month_charges.count()
        
        # This year
        year_charges = Charge.objects.filter(date_paiement__year=now.year)
        year_total = year_charges.aggregate(total=Sum('montant'))['total'] or 0
        year_count = year_charges.count()
        
        # Type distribution
        type_distribution = list(
            Charge.objects.values('type_charge')
            .annotate(total=Sum('montant'), count=Count('id'))
            .order_by('-total')[:10]
        )
        
        # Monthly trend (last 6 months)
        from django.db.models.functions import TruncMonth
        monthly_trend = list(
            Charge.objects.filter(date_paiement__gte=now - timezone.timedelta(days=180))
            .annotate(month=TruncMonth('date_paiement'))
            .values('month')
            .annotate(total=Sum('montant'), count=Count('id'))
            .order_by('month')
        )
        # Format dates for JSON
        for item in monthly_trend:
            if item.get('month'):
                item['month'] = item['month'].strftime('%Y-%m')
        
        return JsonResponse({
            'success': True,
            'stats': {
                'total_all': float(total_all),
                'total_count': total_count,
                'month_total': float(month_total),
                'month_count': month_count,
                'year_total': float(year_total),
                'year_count': year_count,
                'type_distribution': type_distribution,
                'monthly_trend': monthly_trend,
            }
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@csrf_exempt
def api_charges_types(request):
    """API endpoint: GET available charge types.
    
    Returns JSON: { success: True, types: [...] }
    """
    try:
        # Return the main type choices from the ChargeForm
        main_types = [
            {'key': 'travaux', 'label': 'Travaux et entretien'},
            {'key': 'amenagement', 'label': 'Aménagement'},
            {'key': 'vehicule', 'label': 'Dépenses Véhicule'},
            {'key': 'fiscales', 'label': 'Charges Fiscales'},
            {'key': 'fonctionnement', 'label': 'Charge de fonctionnement'},
            {'key': 'autres', 'label': 'Autres frais'},
            {'key': 'abonnement', 'label': 'Charge abonnement'},
        ]
        
        sub_types = {
            'travaux': [
                {'key': 'maconnerie', 'label': 'Maçonnerie'},
                {'key': 'plomberie', 'label': 'Plomberie'},
                {'key': 'carrelage', 'label': 'Carrelage et faïence'},
                {'key': 'peinture', 'label': 'Peinture'},
                {'key': 'courant_fort', 'label': 'Courant fort'},
                {'key': 'courant_faible', 'label': 'Courant faible'},
                {'key': 'menuiserie', 'label': 'Menuiserie générale'},
                {'key': 'finitions', 'label': 'Finitions'},
                {'key': 'soudeur', 'label': 'Soudeur'},
                {'key': 'quincaillerie', 'label': 'Quincaillerie'},
            ],
            'amenagement': [
                {'key': 'mobilier', 'label': 'Mobilier'},
                {'key': 'materiel_equipement', 'label': 'Matériel et équipement'},
            ],
            'vehicule': [
                {'key': 'carburant', 'label': 'Carburant'},
                {'key': 'assurance', 'label': 'Assurance'},
                {'key': 'entretien_mecanique', 'label': 'Entretien et mécanique'},
            ],
            'fiscales': [
                {'key': 'impot', 'label': 'Impôt'},
                {'key': 'cnas', 'label': 'CNAS'},
                {'key': 'casnos', 'label': 'CASNOS'},
                {'key': 'quittance_fiscale', 'label': 'Quittance fiscale'},
            ],
            'fonctionnement': [
                {'key': 'fourniture_bureautique', 'label': 'Fourniture bureautique'},
                {'key': 'impression_photocopie', 'label': 'Impression et photocopie'},
                {'key': 'publicite', 'label': 'Publicité'},
                {'key': 'loyer', 'label': 'Loyer'},
                {'key': 'entretien_nettoyage', 'label': 'Entretien et nettoyage'},
            ],
            'autres': [
                {'key': 'alimentation', 'label': 'Alimentations'},
                {'key': 'cafe_restaurant', 'label': 'Café & Restaurant'},
                {'key': 'voyage', 'label': 'Voyage'},
                {'key': 'transport', 'label': 'Transport'},
                {'key': 'divers', 'label': 'Divers'},
            ],
            'abonnement': [
                {'key': 'facture_electricite', 'label': "Facture d'électricité"},
                {'key': 'facture_gaz', 'label': 'Facture Gaz'},
                {'key': 'facture_internet', 'label': 'Facture internet'},
                {'key': 'facture_telephone', 'label': 'Facture téléphone'},
                {'key': 'facture_TV', 'label': 'Facture TV'},
            ],
        }
        
        return JsonResponse({
            'success': True,
            'main_types': main_types,
            'sub_types': sub_types,
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


def paiements_view(request):
    # List student payments with previous and after balances computed across all inscriptions
    # To avoid slow page loads we limit to the most recent N payments by default and
    # prefetch related inscriptions to avoid N+1 queries. Use ?all=1 to list everything.
    show_all = str(request.GET.get('all') or '') == '1'
    default_limit = 200
    qs = Paiement.objects.select_related('etudiant', 'formation', 'inscription').order_by('-date_paiement', '-id')
    if not show_all:
        qs = qs[:default_limit]

    rows = []
    # Collect etudiant ids to prefetch inscriptions all at once
    etu_ids = set()
    for p in qs:
        if getattr(p, 'etudiant_id', None):
            etu_ids.add(p.etudiant_id)

    inscriptions_map = {}
    if etu_ids:
        ins_qs = Inscription.objects.filter(etudiant_id__in=list(etu_ids)).select_related('formation')
        for ins in ins_qs:
            lst = inscriptions_map.setdefault(ins.etudiant_id, [])
            lst.append(ins)

    # Precompute total_due per student (sum of prix_total or fallback to formation.prix_etudiant)
    total_due_map = {}
    for etu_id, ins_list in inscriptions_map.items():
        total = 0.0
        for ins in ins_list:
            if getattr(ins, 'prix_total', None) is not None and float(ins.prix_total or 0) > 0:
                total += float(ins.prix_total)
            elif getattr(ins, 'formation', None) and getattr(ins.formation, 'prix_etudiant', None) is not None:
                total += float(ins.formation.prix_etudiant)
        total_due_map[etu_id] = total

    # Build prev_paid_map: for each payment pk, store the sum of payments for that student occurring before it
    from collections import defaultdict
    prev_paid_map = {}
    if etu_ids:
        payments_for_students = Paiement.objects.filter(etudiant_id__in=list(etu_ids)).order_by('date_paiement', 'id').values('id', 'etudiant_id', 'montant', 'date_paiement')
        running = defaultdict(float)
        for pay in payments_for_students:
            pid = pay.get('id')
            eid = pay.get('etudiant_id')
            amt = float(pay.get('montant') or 0)
            prev_paid_map[pid] = running.get(eid, 0.0)
            running[eid] = running.get(eid, 0.0) + amt

    debug_count = 0
    # iterate over the (possibly sliced) queryset/list
    for p in qs:
        etu = getattr(p, 'etudiant', None)
        etu_id = etu.id if etu else None
        prenom = getattr(etu, 'prenom', '') or ''
        nom = getattr(etu, 'nom', '') or ''
        etu_email = getattr(etu, 'email', '') or ''
        etu_tel = getattr(etu, 'telephone', '') or ''
        etu_addr = getattr(etu, 'adresse', '') or ''

        # formation display (prefer payment.formation, then inscription, then student default)
        formation_name = ''
        if getattr(p, 'formation', None) and getattr(p.formation, 'nom', None):
            formation_name = p.formation.nom
        else:
            ins_list = inscriptions_map.get(etu_id) if etu_id else None
            if ins_list:
                # prefer the inscription on the payment if present, else the first inscription
                if getattr(p, 'inscription', None) and getattr(p.inscription, 'formation', None) and getattr(p.inscription.formation, 'nom', None):
                    formation_name = p.inscription.formation.nom
                else:
                    first_ins = ins_list[0]
                    formation_name = getattr(first_ins.formation, 'nom', '') or ''
            elif etu and getattr(etu, 'formation', None) and getattr(etu.formation, 'nom', None):
                formation_name = etu.formation.nom

        # compute total_due across inscriptions for this student with fallbacks (use prefetched inscriptions)
        total_due = 0.0
        try:
            if etu_id:
                ins_list = inscriptions_map.get(etu_id, [])
                for ins in ins_list:
                    if getattr(ins, 'prix_total', None) is not None and float(ins.prix_total or 0) > 0:
                        total_due += float(ins.prix_total)
                    elif getattr(ins, 'formation', None) and getattr(ins.formation, 'prix_etudiant', None) is not None:
                        total_due += float(ins.formation.prix_etudiant)
                # include unpaid invoices linked to this student's inscriptions (e.g. frais inscription)
                try:
                    inv_sum = Invoice.objects.filter(inscription__etudiant_id=etu_id).exclude(statut__icontains='pay').aggregate(s=Sum('montant')).get('s') or 0.0
                    total_due += float(inv_sum)
                except Exception:
                    pass
            # fallback to student's default formation price
            if total_due == 0.0 and etu and getattr(etu, 'formation', None) and getattr(etu.formation, 'prix_etudiant', None) is not None:
                total_due = float(etu.formation.prix_etudiant)
        except Exception:
            total_due = 0.0

        # compute balances using precomputed maps
        try:
            total_due_val = total_due_map.get(etu_id, 0.0)
            # prefer freshly computed total_due which includes invoice sums
            if total_due and total_due > total_due_val:
                total_due_val = total_due
            prev_paid = prev_paid_map.get(p.id, 0.0)
            ancien_solde = float(total_due_val) - float(prev_paid or 0.0)
            apres_solde = ancien_solde - float(p.montant or 0)
        except Exception:
            ancien_solde = float(total_due)
            apres_solde = ancien_solde - float(p.montant or 0)

        rows.append({
            'id': p.id,
            'ref': getattr(p, 'reference', None) or f"RET{p.id}",
            'etudiant_id': etu_id,
            'formation_id': p.formation.id if getattr(p, 'formation', None) else None,
            'prenom': prenom,
            'nom': nom,
            'etudiant_nom': f"{prenom} {nom}".strip(),
            'email': etu_email,
            'telephone': etu_tel,
            'adresse': etu_addr,
            'formation': formation_name,
            'date': p.date_paiement.strftime('%Y-%m-%d') if getattr(p, 'date_paiement', None) else '',
            'montant': float(p.montant or 0),
            'ancien_solde': ancien_solde,
            'apres_solde': apres_solde,
            'statut': p.statut or '',
            'mode_paiement': getattr(p, 'mode_paiement', None) or '',
            'numero_cheque': getattr(p, 'numero_cheque', None) or '',
            'date_cheque': (p.date_cheque.strftime('%Y-%m-%d') if getattr(p, 'date_cheque', None) else ''),
            'compte_bancaire': getattr(p, 'compte_bancaire', None) or '',
        })

        # debug logging for first few rows
        if debug_count < 5:
            try:
                print(f"[paiements_view] id={p.id} montant={float(p.montant or 0):.2f} ancien={ancien_solde} apres={apres_solde}")
            except Exception:
                print(f"[paiements_view] id={p.id} debug print failed")
            debug_count += 1

    students = Etudiant.objects.all().order_by('-id')
    formations = Formation.objects.all()
    # minimal role check: detect if current user is 'Secretaire' to adjust UI (hide amounts)
    try:
        uid = request.session.get('user_id') or None
        current_user = Utilisateur.objects.filter(id=uid).first() if uid else None
        role = (getattr(current_user, 'role', '') or '').strip().lower() if current_user else ''
        is_secretaire = role in ('secretaire', 'secrétaire')
    except Exception:
        is_secretaire = False

    return render(request, 'paiements.html', {'paiements': rows, 'students': students, 'formations': formations, 'is_secretaire': is_secretaire})


def paiement_receipt(request, pk):
    """Render a printable receipt for a Paiement by primary key."""
    p = get_object_or_404(Paiement, pk=pk)
    # try to build friendly display values
    try:
        montant = float(p.montant)
    except Exception:
        montant = p.montant
    # compute balances for this payment (ancien/apres)
    # Use the same logic as in `paiements_view` so the printed receipt matches the payments list.
    ancien_solde = None
    apres_solde = None
    try:
        etu_id = p.etudiant.id if p.etudiant else None
        # compute total_due from inscriptions (prefer prix_total, fallback to formation.prix_etudiant)
        total_due = 0.0
        if etu_id is not None:
            ins_qs = Inscription.objects.filter(etudiant_id=etu_id).select_related('formation')
            for ins in ins_qs:
                if getattr(ins, 'prix_total', None) is not None and float(ins.prix_total or 0) > 0:
                    total_due += float(ins.prix_total)
                elif getattr(ins, 'formation', None) and getattr(ins.formation, 'prix_etudiant', None) is not None:
                    total_due += float(ins.formation.prix_etudiant)
            # include unpaid invoices linked to this student's inscriptions (e.g. frais inscription)
            try:
                inv_sum = Invoice.objects.filter(inscription__etudiant_id=etu_id).exclude(statut__icontains='pay').aggregate(s=Sum('montant')).get('s') or 0.0
                total_due += float(inv_sum)
            except Exception:
                pass

        # compute previous paid amount (payments strictly before this payment in ordering)
        prev_paid = 0.0
        try:
            qs = Paiement.objects.filter(etudiant_id=etu_id)
            qs = qs.exclude(pk=p.pk)
            if p.date_paiement is not None:
                from django.db.models import Q
                before_q = Q(date_paiement__lt=p.date_paiement)
                same_day_q = Q(date_paiement=p.date_paiement) & Q(id__lt=p.pk)
                qs = qs.filter(before_q | same_day_q)
            prev_paid = float(qs.aggregate(s=Sum('montant')).get('s') or 0.0)
        except Exception:
            prev_paid = 0.0

        ancien_solde = float(total_due) - float(prev_paid or 0.0)
        apres_solde = ancien_solde - float(p.montant or 0)
    except Exception:
        ancien_solde = getattr(p, 'ancien_solde', None)
        apres_solde = getattr(p, 'apres_solde', None)

    # prepare list of formations that this payment concerns.
    # Prefer explicit links: if the Paiement references an Inscription, show that formation only;
    # otherwise, if Paiement.formation is set, show it. Do NOT list all student's inscriptions here.
    formations_list = []
    try:
        # If the payment is tied to a specific inscription, show that formation
        if getattr(p, 'inscription', None) and getattr(p.inscription, 'formation', None):
            fname = getattr(p.inscription.formation, 'nom', None)
            if fname:
                formations_list = [fname]
        # else if the payment has a direct formation FK, show it
        elif getattr(p, 'formation', None):
            fname = getattr(p.formation, 'nom', None)
            if fname:
                formations_list = [fname]
        # otherwise leave empty (safer than listing all student's inscriptions)
        else:
            formations_list = []
    except Exception:
        formations_list = []

    # compute status label using the same logic as in paiements.html
    statut_label = ''
    try:
        if apres_solde is not None:
            a = float(apres_solde)
            anc = float(ancien_solde) if ancien_solde is not None else None
            if a == 0:
                statut_label = 'Réglé'
            elif anc is not None and a < anc:
                statut_label = 'Partiellement réglé'
            else:
                statut_label = 'Non réglé'
        else:
            statut_label = p.statut or ''
    except Exception:
        statut_label = p.statut or ''

    context = {
        'paiement': p,
        'montant': montant,
        'date': p.date_paiement or (p.created_at if hasattr(p, 'created_at') else None),
        'etudiant': p.etudiant,
    'formation': p.formation,
    'formations_list': formations_list,
    'ancien_solde': ancien_solde,
    'apres_solde': apres_solde,
    'statut_label': statut_label,
    'frais_inscription_included': True if (getattr(p, 'remarques', '') and 'Frais inscription' in str(getattr(p, 'remarques', ''))) else False,
    'frais_inscription_amount': 500.00 if (getattr(p, 'remarques', '') and 'Frais inscription' in str(getattr(p, 'remarques', ''))) else None,
    }

    # Compute total_frais robustly and add to context (debug-friendly)
    total_frais_val = None
    parts = []
    try:
        if p.etudiant:
            ins_qs = Inscription.objects.filter(etudiant_id=p.etudiant.id).select_related('formation')
            for ins in ins_qs:
                if getattr(ins, 'prix_total', None) is not None:
                    v = float(ins.prix_total or 0)
                else:
                    v = float(getattr(ins.formation, 'prix_etudiant', 0) or 0) if getattr(ins, 'formation', None) else 0.0
                parts.append(v)
            total_frais_val = sum(parts)
    except Exception:
        total_frais_val = None

    try:
        print(f"[paiement_receipt] paiement_id={p.pk} inscriptions_count={len(parts)} total_frais={total_frais_val}")
    except Exception:
        pass

    context['total_frais'] = total_frais_val
    return render(request, 'paiement_receipt.html', context)


def reglements_fournisseurs_view(request):
    """Supplier settlements dashboard: list fournisseurs, achats, reglements and provide
    JSON endpoints for creating/updating them via AJAX from the front-end."""
    from django.views.decorators.http import require_POST
    from django.db import utils as db_utils

    db_error = None
    fournisseurs = []
    achats = []
    reglements = []
    total_achats = 0
    total_reglements = 0
    try:
        # force evaluation here so any DB errors occur inside the try block
        fournisseurs = list(Fournisseur.objects.all().order_by('nom'))
        achats = list(Achat.objects.select_related('fournisseur').all().order_by('-date_achat')[:100])
        reglements = list(ReglementFournisseur.objects.select_related('fournisseur', 'achat').all().order_by('-date_reglement')[:100])

        # quick stats
        total_achats = Achat.objects.aggregate(total=Sum('total'))['total'] or 0
        total_reglements = ReglementFournisseur.objects.aggregate(total=Sum('montant'))['total'] or 0
    except (db_utils.ProgrammingError, db_utils.OperationalError) as e:
        db_error = str(e)

    context = {
        'fournisseurs': fournisseurs,
        'achats': achats,
        'reglements': reglements,
        'total_achats': total_achats,
        'total_reglements': total_reglements,
        'db_error': db_error,
    }
    return render(request, 'reglements_fournisseurs.html', context)


@csrf_exempt
def api_fournisseur_create(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST only'}, status=405)
    data = json.loads(request.body.decode('utf-8') or '{}')
    nom = data.get('nom')
    if not nom:
        return JsonResponse({'success': False, 'error': 'Missing nom'}, status=400)
    f = Fournisseur.objects.create(
        nom=nom,
        contact=data.get('contact'),
        email=data.get('email'),
        telephone=data.get('telephone'),
        adresse=data.get('adresse'),
        remarques=data.get('remarques'),
    )
    return JsonResponse({'success': True, 'id': f.id, 'nom': f.nom})


@csrf_exempt
def api_achat_create(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST only'}, status=405)
    data = json.loads(request.body.decode('utf-8') or '{}')
    fournisseur_id = data.get('fournisseur_id')
    try:
        fournisseur = Fournisseur.objects.get(pk=int(fournisseur_id))
    except Exception:
        return JsonResponse({'success': False, 'error': 'Invalid fournisseur'}, status=400)
    a = Achat.objects.create(
        fournisseur=fournisseur,
        date_achat=data.get('date_achat') or datetime.now().date(),
        total=data.get('total') or 0,
        remarques=data.get('remarques'),
    )
    # optional items
    items = data.get('items') or []
    for it in items:
        AchatItem.objects.create(achat=a, description=it.get('description') or '', quantite=it.get('quantite') or 1, prix_unitaire=it.get('prix_unitaire') or 0)
    return JsonResponse({'success': True, 'id': a.id})


@csrf_exempt
def api_reglement_fournisseur_create(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST only'}, status=405)
    data = json.loads(request.body.decode('utf-8') or '{}')
    fournisseur_id = data.get('fournisseur_id')
    try:
        fournisseur = Fournisseur.objects.get(pk=int(fournisseur_id))
    except Exception:
        return JsonResponse({'success': False, 'error': 'Invalid fournisseur'}, status=400)
    achat = None
    if data.get('achat_id'):
        try:
            achat = Achat.objects.get(pk=int(data.get('achat_id')))
        except Exception:
            achat = None
    montant = data.get('montant')
    try:
        montant = float(montant)
    except Exception:
        return JsonResponse({'success': False, 'error': 'Invalid montant'}, status=400)
    rf = ReglementFournisseur.objects.create(fournisseur=fournisseur, achat=achat, montant=montant, date_reglement=data.get('date_reglement') or datetime.now().date(), mode=data.get('mode'), remarques=data.get('remarques'))
    return JsonResponse({'success': True, 'id': rf.id})


def add_paiement(request):
    # Collect request-level debug info (useful when fetch/ajax form setups behave oddly)
    # Build request debug info carefully; avoid reading request.body if the stream has been consumed
    body_len = None
    body_preview = ''
    try:
        body_len = len(request.body or b'')
        if body_len:
            body_preview = request.body[:200].decode(errors='replace')
    except Exception:
        # RawPostDataException or similar: can't re-read body
        body_len = None
        body_preview = '<unavailable - stream already read>'

    request_debug = {
        'method': request.method,
        'path': getattr(request, 'path', None),
        'full_path': getattr(request, 'get_full_path', lambda: '')(),
        'host': request.META.get('HTTP_HOST'),
        'referer': request.META.get('HTTP_REFERER'),
        'remote_addr': request.META.get('REMOTE_ADDR'),
        'content_type': request.META.get('CONTENT_TYPE') or request.META.get('HTTP_CONTENT_TYPE'),
        'content_length': request.META.get('CONTENT_LENGTH'),
        'post_keys': list(request.POST.keys()),
        'files_keys': list(request.FILES.keys()),
        'has_csrf_token': bool(request.POST.get('csrfmiddlewaretoken') or request.META.get('HTTP_X_CSRFTOKEN')),
        'body_length': body_len,
        'body_preview': body_preview,
    }

    # Accept typical POST requests, but if Django's request.method is not POST
    # still try to handle the request when form data is present (some fetch/form setups
    # may lead to method reporting issues). If nothing is present, return Invalid method
    # along with the request_debug blob to aid investigation.




    
    if request.method != 'POST' and not (request.POST or request.FILES or request.body):
        return JsonResponse({'success': False, 'error': 'Invalid method', 'request_debug': request_debug}, status=405)
    try:
        # Resolve student (id or email)
        etu_field = request.POST.get('etudiant')
        if not etu_field:
            return JsonResponse({'success': False, 'error': 'Missing etudiant'}, status=400)
        etu = None
        try:
            etu_id = int(etu_field)
            etu = Etudiant.objects.filter(id=etu_id).first()
        except Exception:
            etu = Etudiant.objects.filter(email__iexact=etu_field).first()
            if not etu:
                # try by name
                parts = etu_field.strip().split()
                if len(parts) >= 2:
                    etu = Etudiant.objects.filter(nom__iexact=parts[-1], prenom__iexact=' '.join(parts[:-1])).first()
                if not etu:
                    etu = Etudiant.objects.filter(nom__icontains=etu_field).first()
        if not etu:
            return JsonResponse({'success': False, 'error': 'Etudiant introuvable'}, status=404)

        # resolve formation if provided
        formation = None
        formation_field = request.POST.get('formation')
        if formation_field:
            try:
                formation_id = int(formation_field)
                formation = Formation.objects.filter(id=formation_id).first()
            except Exception:
                formation = Formation.objects.filter(nom__iexact=formation_field).first()

        # optional inscription
        inscription = None
        ins_field = request.POST.get('inscription')
        if ins_field:
            try:
                ins_id = int(ins_field)
                inscription = Inscription.objects.filter(id=ins_id).first()
            except Exception:
                inscription = Inscription.objects.filter(etudiant=etu, formation__nom__iexact=ins_field).first()

        montant = request.POST.get('montant')
        if not montant:
            return JsonResponse({'success': False, 'error': 'Missing montant'}, status=400)
        try:
            montant = Decimal(str(montant))
        except Exception:
            return JsonResponse({'success': False, 'error': 'Invalid montant'}, status=400)

        # Optional: include registration fee (fixed 500 DZD) when requested by the form
        include_inscription_fee = bool(request.POST.get('include_inscription_fee'))
        inscription_fee_amount = Decimal('500.00') if include_inscription_fee else Decimal('0.00')
        # We DO NOT add the fee to the payment montant. Instead we create an Invoice
        # representing the additional charge so the student's real remaining due increases.
        remarques_to_save = (request.POST.get('remarques') or '').strip()
        # date is set to today automatically
        date_p = datetime.now().date()

        # Determine formation: explicit selection > inscription's formation > student's default formation
        resolved_formation = formation or (inscription.formation if inscription else None) or getattr(etu, 'formation', None)

        # read optional payment metadata from form
        mode_val = request.POST.get('mode_paiement') or request.POST.get('mode') or request.POST.get('modePaiement')
        numero_cheque_val = request.POST.get('numero_cheque') or request.POST.get('numero-cheque') or request.POST.get('numeroCheque')
        date_cheque_val = request.POST.get('date_cheque') or request.POST.get('date-cheque') or request.POST.get('dateCheque')
        compte_bancaire_val = request.POST.get('compte_bancaire') or request.POST.get('compte-bancaire') or request.POST.get('compteBancaire')

        # parse date_cheque if provided
        parsed_date_cheque = None
        if date_cheque_val:
            try:
                parsed_date_cheque = datetime.fromisoformat(date_cheque_val).date()
            except Exception:
                try:
                    parsed_date_cheque = datetime.strptime(date_cheque_val, '%Y-%m-%d').date()
                except Exception:
                    parsed_date_cheque = None

        # Create payment and persist optional metadata
        p = Paiement.objects.create(
            etudiant=etu,
            formation=resolved_formation,
            inscription=inscription,
            montant=montant,
            date_paiement=date_p,
            mode_paiement=mode_val or None,
            numero_cheque=numero_cheque_val or None,
            date_cheque=parsed_date_cheque,
            compte_bancaire=compte_bancaire_val or None,
            remarques=remarques_to_save or None,
        )

    # compute status based on balance_after (signal will have set it)
        # reload instance
        p.refresh_from_db()
        # Ensure balance_after calculated (use shared util)
        try:
            if p.balance_after is None:
                res = calculate_balances(etu.id, montant=p.montant, exclude_payment_pk=p.pk)
                p.balance_after = res.get('apres_solde')
        except Exception:
            pass
        apres = float(p.balance_after or 0.0)
        p.statut = 'Non réglé' if apres > 0 else 'Réglé'
        p.save()

        # re-fetch to include fields set by signals (reference, balance_after)
        p = Paiement.objects.select_related('etudiant', 'formation', 'inscription').get(pk=p.pk)
        # If student has an email, schedule sending the paiement receipt (background)
        try:
            to_email = (p.etudiant.email or '').strip()
            if to_email:
                # Build a minimal context similar to paiement_receipt_pdf
                try:
                    montant_val = float(p.montant)
                except Exception:
                    montant_val = p.montant
                try:
                    balances = calculate_balances(p.etudiant.id if p.etudiant else None, payment_date=p.date_paiement, montant=p.montant, exclude_payment_pk=p.pk)
                    ancien_solde = balances.get('ancien_solde')
                    apres_solde = balances.get('apres_solde')
                except Exception:
                    ancien_solde = getattr(p, 'ancien_solde', None)
                    apres_solde = getattr(p, 'apres_solde', None)
                formations_list = []
                try:
                    if getattr(p, 'inscription', None) and getattr(p.inscription, 'formation', None):
                        fname = getattr(p.inscription.formation, 'nom', None)
                        if fname:
                            formations_list = [fname]
                    elif getattr(p, 'formation', None):
                        fname = getattr(p.formation, 'nom', None)
                        if fname:
                            formations_list = [fname]
                except Exception:
                    formations_list = []
                email_ctx = {
                    'paiement': p,
                    'etudiant': p.etudiant,
                    'formation': getattr(p, 'formation', None),
                    'formations_list': formations_list,
                    'montant': montant_val,
                    'total_frais': getattr(p, 'prix_total', None),
                    'ancien_solde': ancien_solde,
                    'apres_solde': apres_solde,
                    'statut_label': p.statut,
                    'frais_inscription_included': True if (getattr(p, 'remarques', '') and 'Frais inscription' in str(getattr(p, 'remarques', ''))) else False,
                    'frais_inscription_amount': 500.00 if (getattr(p, 'remarques', '') and 'Frais inscription' in str(getattr(p, 'remarques', ''))) else None,
                }
                try:
                    _write_email_log(f"SCHEDULING_PAIEMENT to={to_email} paiement_id={p.id}")
                    t = send_paiement_receipt_async(to_email, email_ctx, force_smtp=True)
                    if not t:
                        _write_email_log(f"SCHEDULING_PAIEMENT_FAILED to={to_email} paiement_id={p.id}")
                    else:
                        _write_email_log(f"SCHEDULING_PAIEMENT_OK to={to_email} paiement_id={p.id}")
                        # Mirror welcome email behavior: also trigger an immediate smtplib send in background
                        try:
                            _write_email_log(f"UI_TRIGGER_SMTP_SEND paiement to={to_email} paiement_id={p.id}")
                            subject = "Votre reçu de paiement - GénieSchool"
                            html_message = render_to_string('paiement_receipt.html', email_ctx)
                            plain_message = strip_tags(html_message)
                            _smtplib_send_direct_thread(to_email, subject=subject, html_message=html_message, plain_message=plain_message)
                        except Exception as e_smtp:
                            _write_email_log(f"UI_TRIGGER_SMTP_FAIL paiement to={to_email} paiement_id={p.id} err={e_smtp}")
                except Exception as e:
                    _write_email_log(f"SCHEDULING_PAIEMENT_EXC to={to_email} paiement_id={getattr(p,'id',None)} err={e}")
                    pass
        except Exception:
            _write_email_log(f"PAIEMENT_EMAIL_BLOCK_FAILED paiement_id={getattr(p,'id',None)}")
            pass
        invoice_info = None
        # create an Invoice for the registration fee (unpaid) so it increases the student's due
        if include_inscription_fee:
            try:
                inv_inscription = inscription
                if not inv_inscription:
                    # try to attach to the student's latest inscription so the invoice is associated with the etudiant
                    inv_inscription = Inscription.objects.filter(etudiant=etu).order_by('-date_inscription').first()
                inv = Invoice.objects.create(
                    inscription=inv_inscription,
                    paiement=None,
                    numero=None,
                    montant=inscription_fee_amount,
                    date_emission=datetime.now(),
                    statut='emi',
                    remarques='Frais inscription auto ajouté via interface',
                )
                invoice_info = {'id': inv.id, 'montant': str(inv.montant)}
            except Exception:
                invoice_info = None
        data = {
            'id': p.id,
            'reference': p.reference,
            'etudiant': {'id': p.etudiant.id, 'nom': p.etudiant.nom, 'prenom': p.etudiant.prenom},
            'montant': str(p.montant),
            'date_paiement': p.date_paiement.isoformat() if p.date_paiement else None,
            'mode_paiement': p.mode_paiement,
            'numero_cheque': (p.numero_cheque if p.numero_cheque else None),
            'date_cheque': (p.date_cheque.isoformat() if p.date_cheque else None),
            'compte_bancaire': p.compte_bancaire,
            'balance_after': str(p.balance_after) if p.balance_after is not None else None,
            'statut': p.statut,
            'debug_balance': calculate_balances(p.etudiant.id, montant=p.montant, exclude_payment_pk=p.pk),
            'frais_inscription_included': include_inscription_fee,
            'frais_inscription_amount': str(inscription_fee_amount) if include_inscription_fee else None,
            'frais_inscription_invoice': invoice_info,
        }
        return JsonResponse({'success': True, 'paiement': data})
    except Exception as e:
        import traceback
        traceback.print_exc()
        # include request_debug when available to help diagnose issues from the client
        try:
            debug_blob = request_debug
        except Exception:
            debug_blob = None
        resp = {'success': False, 'error': str(e)}
        if debug_blob is not None:
            resp['request_debug'] = debug_blob
        return JsonResponse(resp, status=500)


def edit_paiement(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Invalid method'}, status=405)
    try:
        try:
            pid = int(request.POST.get('id') or request.POST.get('_id'))
        except Exception:
            return JsonResponse({'success': False, 'error': 'Missing id'}, status=400)
        p = Paiement.objects.filter(id=pid).first()
        if not p:
            return JsonResponse({'success': False, 'error': 'Paiement introuvable'}, status=404)

    # update fields if present
        if request.POST.get('montant'):
            try:
                p.montant = Decimal(str(request.POST.get('montant')))
            except Exception:
                pass
        # allow updating montant and date only; status will be recalculated
        if request.POST.get('date_paiement'):
            try:
                p.date_paiement = datetime.fromisoformat(request.POST.get('date_paiement')).date()
            except Exception:
                try:
                    p.date_paiement = datetime.strptime(request.POST.get('date_paiement'), '%Y-%m-%d').date()
                except Exception:
                    pass

        # optional: change etudiant/formation/inscription by id
        if request.POST.get('etudiant'):
            try:
                etu_id = int(request.POST.get('etudiant'))
                etu = Etudiant.objects.filter(id=etu_id).first()
                if etu:
                    p.etudiant = etu
            except Exception:
                pass
        if request.POST.get('formation'):
            try:
                fid = int(request.POST.get('formation'))
                f = Formation.objects.filter(id=fid).first()
                if f:
                    p.formation = f
            except Exception:
                pass
        if request.POST.get('inscription'):
            try:
                iid = int(request.POST.get('inscription'))
                ins = Inscription.objects.filter(id=iid).first()
                if ins:
                    p.inscription = ins
            except Exception:
                pass

        # optional: update payment metadata (mode, cheque, virement account)
        if request.POST.get('mode_paiement') or request.POST.get('mode'):
            p.mode_paiement = request.POST.get('mode_paiement') or request.POST.get('mode')
        if request.POST.get('numero_cheque') or request.POST.get('numero-cheque'):
            p.numero_cheque = request.POST.get('numero_cheque') or request.POST.get('numero-cheque')
        if request.POST.get('date_cheque') or request.POST.get('date-cheque'):
            dc = request.POST.get('date_cheque') or request.POST.get('date-cheque')
            try:
                p.date_cheque = datetime.fromisoformat(dc).date()
            except Exception:
                try:
                    p.date_cheque = datetime.strptime(dc, '%Y-%m-%d').date()
                except Exception:
                    pass
        if request.POST.get('compte_bancaire') or request.POST.get('compte-bancaire'):
            p.compte_bancaire = request.POST.get('compte_bancaire') or request.POST.get('compte-bancaire')

        p.save()
        # recalc status based on balance_after (signal updates balance_after on save)
        p.refresh_from_db()
        try:
            if p.balance_after is None:
                res = calculate_balances(p.etudiant.id, montant=p.montant, exclude_payment_pk=p.pk)
                p.balance_after = res.get('apres_solde')
        except Exception:
            pass
        apres = float(p.balance_after or 0.0)
        p.statut = 'Non réglé' if apres > 0 else 'Réglé'
        p.save()
        # re-fetch
        p = Paiement.objects.select_related('etudiant', 'formation', 'inscription').get(pk=p.pk)
        data = {
            'id': p.id,
            'reference': p.reference,
            'etudiant': {'id': p.etudiant.id, 'nom': p.etudiant.nom, 'prenom': p.etudiant.prenom},
            'montant': str(p.montant),
            'date_paiement': p.date_paiement.isoformat() if p.date_paiement else None,
            'mode_paiement': p.mode_paiement,
            'numero_cheque': p.numero_cheque,
            'date_cheque': (p.date_cheque.isoformat() if p.date_cheque else None),
            'compte_bancaire': p.compte_bancaire,
            'balance_after': str(p.balance_after) if p.balance_after is not None else None,
            'statut': p.statut,
        }
        return JsonResponse({'success': True, 'paiement': data})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


def enseignants_view(request):
    # Simple list view for enseignants using same table layout as etudiants (no card view)
    from django.core.paginator import Paginator
    # Use the dedicated Enseignant model only
    qs = Enseignant.objects.all().order_by('-id')
    try:
        per_page = int(request.GET.get('per_page') or 20)
    except Exception:
        per_page = 20
    page_number = request.GET.get('page') or 1
    paginator = Paginator(qs, per_page)
    page_obj = paginator.get_page(page_number)
    # prepare photo_url attribute (like etudiants_view)
    for u in page_obj:
        u.photo_url = ''
        try:
            photo_field = getattr(u, 'photo', None)
            if photo_field:
                # try storage.url first
                try:
                    u.photo_url = default_storage.url(photo_field)
                except Exception:
                    try:
                        # if FileField, use .url
                        u.photo_url = photo_field.url if getattr(photo_field, 'url', None) else (settings.MEDIA_URL + str(photo_field))
                    except Exception:
                        u.photo_url = str(photo_field)
        except Exception:
            u.photo_url = getattr(u, 'photo', '') or ''
        # ensure compte_bancaire attribute exists for templates (may be absent on older schemas)
        try:
            setattr(u, 'compte_bancaire', getattr(u, 'compte_bancaire', '') or '')
        except Exception:
            try:
                # fallback: ensure attribute present
                u.compte_bancaire = ''
            except Exception:
                pass
        # compute display status: consider 'Actif' when currently assigned to a formation
        try:
            u.display_statut = 'Actif' if getattr(u, 'formation', None) else (u.statut or 'Inactif')
        except Exception:
            u.display_statut = u.statut or 'Inactif'
    # provide list of formation names for the specialite/niveau combos
    try:
        formations_list = list(Formation.objects.values_list('nom', flat=True))
    except Exception:
        formations_list = []

    return render(request, 'enseignants.html', {
        'enseignants': page_obj,
        'paginator': paginator,
        'page_obj': page_obj,
        'is_paginated': page_obj.has_other_pages(),
        'per_page': per_page,
        'formations_list': formations_list,
    })


def add_enseignant(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Invalid method'}, status=405)
    nom = request.POST.get('nom') or ''
    prenom = request.POST.get('prenom') or ''
    email = request.POST.get('email')
    telephone = request.POST.get('telephone')
    sexe = request.POST.get('sexe') or None
    nationalite = request.POST.get('nationalite') or None
    specialite = request.POST.get('specialite') or None
    bio = request.POST.get('bio') or None
    niveau = request.POST.get('niveau') or None
    # optional personal fields
    date_naissance_str = request.POST.get('date_naissance') or None
    lieu_naissance = request.POST.get('lieu_naissance') or None
    salaire_val = request.POST.get('salaire_valeur') or None
    compte_bancaire = request.POST.get('compte_bancaire') or None
    # save photo if provided
    photo_field = None
    if request.FILES.get('photo'):
        f = request.FILES['photo']
        path = default_storage.save(f'enseignants/{f.name}', ContentFile(f.read()))
        photo_field = path
    adresse = request.POST.get('adresse')
    # create Enseignant (include optional fields when model supports them)
    create_kwargs = dict(nom=nom, prenom=prenom, email=email, telephone=telephone, adresse=adresse or '', photo=photo_field)
    # prefer to set known model fields at create time
    if hasattr(Enseignant, 'sexe') and sexe is not None:
        create_kwargs['sexe'] = sexe
    if hasattr(Enseignant, 'specialite') and specialite is not None:
        create_kwargs['specialite'] = specialite
    if hasattr(Enseignant, 'niveau') and niveau is not None:
        create_kwargs['niveau'] = niveau
    if hasattr(Enseignant, 'bio') and bio is not None:
        create_kwargs['bio'] = bio
    # include date_naissance / lieu_naissance / salaire at creation when model supports them
    if date_naissance_str and hasattr(Enseignant, 'date_naissance'):
        try:
            from datetime import datetime
            create_kwargs['date_naissance'] = datetime.fromisoformat(date_naissance_str).date()
        except Exception:
            try:
                create_kwargs['date_naissance'] = datetime.strptime(date_naissance_str, '%Y-%m-%d').date()
            except Exception:
                pass
    if lieu_naissance is not None and hasattr(Enseignant, 'lieu_naissance'):
        create_kwargs['lieu_naissance'] = lieu_naissance or None
    if salaire_val is not None and hasattr(Enseignant, 'salaire'):
        try:
            from decimal import Decimal
            create_kwargs['salaire'] = Decimal(str(salaire_val))
        except Exception:
            pass
    if compte_bancaire is not None and hasattr(Enseignant, 'compte_bancaire'):
        create_kwargs['compte_bancaire'] = compte_bancaire or None
    u = Enseignant.objects.create(**create_kwargs)
    # persist nationalite if model has the field; otherwise we'll return it in JSON only
    if nationalite is not None and hasattr(u, 'nationalite'):
        try:
            setattr(u, 'nationalite', nationalite)
            u.save()
        except Exception:
            pass
    # compute returned photo url safely
    photo_url = ''
    try:
        if getattr(u, 'photo', None):
            try:
                photo_url = default_storage.url(u.photo)
            except Exception:
                try:
                    photo_url = u.photo.url if getattr(u.photo, 'url', None) else (settings.MEDIA_URL + str(u.photo))
                except Exception:
                    photo_url = str(u.photo)
    except Exception:
        photo_url = getattr(u, 'photo', '') or ''

    data = {
        'id': u.id,
        'nom': u.nom,
        'prenom': u.prenom,
        'email': u.email,
        'telephone': u.telephone,
        'adresse': u.adresse or '',
        'photo': photo_url,
        'statut': u.statut,
        'display_statut': 'Actif' if u.formation else (u.statut or 'Inactif'),
    'sexe': getattr(u, 'sexe', '') or (sexe or ''),
    'specialite': getattr(u, 'specialite', '') or (specialite or ''),
    'niveau': getattr(u, 'niveau', '') or (niveau or ''),
    'bio': getattr(u, 'bio', '') or (bio or ''),
    'nationalite': getattr(u, 'nationalite', '') or (nationalite or ''),
    'date_naissance': (u.date_naissance.isoformat() if getattr(u, 'date_naissance', None) else None),
    'lieu_naissance': getattr(u, 'lieu_naissance', '') or (lieu_naissance or ''),
    'salaire': (str(u.salaire) if getattr(u, 'salaire', None) is not None else ''),
    'compte_bancaire': getattr(u, 'compte_bancaire', '') or (compte_bancaire or ''),
    }
    return JsonResponse({'success': True, 'enseignant': data})


def edit_enseignant(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Invalid method'}, status=405)
    try:
        uid = int(request.POST.get('id') or request.POST.get('_id'))
    except Exception:
        return JsonResponse({'success': False, 'error': 'Missing id'}, status=400)
    u = Enseignant.objects.filter(id=uid).first()
    if not u:
        return JsonResponse({'success': False, 'error': 'Not found'}, status=404)
    u.nom = request.POST.get('nom') or u.nom
    u.prenom = request.POST.get('prenom') or u.prenom
    u.email = request.POST.get('email') or u.email
    u.telephone = request.POST.get('telephone') or u.telephone
    # new optional fields
    u.adresse = request.POST.get('adresse') or u.adresse
    if request.POST.get('sexe') is not None:
        if hasattr(u, 'sexe'):
            u.sexe = request.POST.get('sexe') or None
    if request.POST.get('specialite') is not None:
        if hasattr(u, 'specialite'):
            u.specialite = request.POST.get('specialite') or None
    if request.POST.get('niveau') is not None:
        if hasattr(u, 'niveau'):
            u.niveau = request.POST.get('niveau') or None
    if request.POST.get('bio') is not None:
        if hasattr(u, 'bio'):
            u.bio = request.POST.get('bio') or None
    # optional: date_naissance, lieu_naissance, salaire, compte_bancaire
    if request.POST.get('date_naissance') is not None and hasattr(u, 'date_naissance'):
        try:
            from datetime import datetime
            u.date_naissance = datetime.fromisoformat(request.POST.get('date_naissance')).date()
        except Exception:
            try:
                u.date_naissance = datetime.strptime(request.POST.get('date_naissance'), '%Y-%m-%d').date()
            except Exception:
                pass
    if request.POST.get('lieu_naissance') is not None and hasattr(u, 'lieu_naissance'):
        u.lieu_naissance = request.POST.get('lieu_naissance') or None
    if request.POST.get('salaire_valeur') is not None and hasattr(u, 'salaire'):
        try:
            from decimal import Decimal
            u.salaire = Decimal(str(request.POST.get('salaire_valeur')))
        except Exception:
            pass
    if request.POST.get('compte_bancaire') is not None and hasattr(u, 'compte_bancaire'):
        try:
            setattr(u, 'compte_bancaire', request.POST.get('compte_bancaire') or None)
        except Exception:
            pass
    # nationalite: persist only if model supports it
    nationalite_value = request.POST.get('nationalite') if request.POST.get('nationalite') is not None else None
    if nationalite_value is not None and hasattr(u, 'nationalite'):
        try:
            setattr(u, 'nationalite', nationalite_value or None)
        except Exception:
            pass
    if request.FILES.get('photo'):
        f = request.FILES['photo']
        path = default_storage.save(f'enseignants/{f.name}', ContentFile(f.read()))
        u.photo = path
    u.save()
    # compute returned photo url safely
    photo_url = ''
    try:
        if getattr(u, 'photo', None):
            try:
                photo_url = default_storage.url(u.photo)
            except Exception:
                try:
                    photo_url = u.photo.url if getattr(u.photo, 'url', None) else (settings.MEDIA_URL + str(u.photo))
                except Exception:
                    photo_url = str(u.photo)
    except Exception:
        photo_url = getattr(u, 'photo', '') or ''

    data = {
        'id': u.id,
        'nom': u.nom,
        'prenom': u.prenom,
        'email': u.email,
        'telephone': u.telephone,
    'adresse': u.adresse or '',
        'photo': photo_url,
        'statut': u.statut,
    'display_statut': 'Actif' if u.formation else (u.statut or 'Inactif'),
    'sexe': getattr(u, 'sexe', '') or '',
    'specialite': getattr(u, 'specialite', '') or '',
    'niveau': getattr(u, 'niveau', '') or '',
    'bio': getattr(u, 'bio', '') or '',
    'nationalite': getattr(u, 'nationalite', '') or (nationalite_value or ''),
    'date_naissance': (u.date_naissance.isoformat() if getattr(u, 'date_naissance', None) else None),
    'lieu_naissance': getattr(u, 'lieu_naissance', '') or '',
    'salaire': (str(u.salaire) if getattr(u, 'salaire', None) is not None else ''),
    'compte_bancaire': getattr(u, 'compte_bancaire', '') or '',
    }
    return JsonResponse({'success': True, 'enseignant': data})


def delete_enseignant(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Invalid method'}, status=405)
    try:
        uid = int(request.POST.get('id') or request.POST.get('_id'))
    except Exception:
        return JsonResponse({'success': False, 'error': 'Missing id'}, status=400)
    u = Enseignant.objects.filter(id=uid).first()
    if not u:
        return JsonResponse({'success': False, 'error': 'Not found'}, status=404)
    u.delete()
    return JsonResponse({'success': True})


def rapports_view(request):
    reports = Invoice.objects.count()
    # helper to make a web-friendly url for media/file fields
    def _make_url(p):
        if not p:
            return ''
        try:
            url = default_storage.url(p)
        except Exception:
            url = (settings.MEDIA_URL or '/') + (p or '')
        url = url.replace('\\', '/')
        return url

    # Serialize students into JSON-serializable dicts for json_script usage in template
    students_serialized = []
    try:
        qs = Etudiant.objects.select_related('formation').prefetch_related('paiements').all()
        for s in qs:
            # gather payments if any
            payments = []
            try:
                for p in getattr(s, 'paiements').all():
                    payments.append({
                        'id': p.id,
                        'amount': float(p.montant) if p.montant is not None else None,
                        'date': p.date_paiement.isoformat() if getattr(p, 'date_paiement', None) else None,
                        'formation_id': p.formation.id if getattr(p, 'formation', None) else None,
                        'formation_name': p.formation.nom if getattr(p, 'formation', None) else None,
                    })
            except Exception:
                payments = []

            students_serialized.append({
                'id': s.id,
                'nom': s.nom,
                'prenom': s.prenom,
                'nom_arabe': getattr(s, 'nom_arabe', '') or '',
                'prenom_arabe': getattr(s, 'prenom_arabe', '') or '',
                'sexe': getattr(s, 'sexe', '') or '',
                'email': getattr(s, 'email', '') or '',
                'telephone': getattr(s, 'telephone', '') or '',
                'adresse': getattr(s, 'adresse', '') or '',
                'photo_url': _make_url(getattr(s, 'photo', '') or getattr(s, 'photo', '') or ''),
                'date_inscription': (s.date_inscription.isoformat() if getattr(s, 'date_inscription', None) else None),
                'situation': getattr(s, 'situation_professionnelle', '') or '',
                'statut': getattr(s, 'statut', '') or '',
                'verification_step': getattr(s, 'verification_step', 0) or 0,
                'inscription_status': 'inscrit' if s.inscriptions.exists() else 'non_inscrit',
                'formation': s.formation.id if getattr(s, 'formation', None) else None,
                'formation_text': getattr(s, 'formation_text', '') or (s.formation.nom if getattr(s, 'formation', None) else ''),
                'payments': payments,
            })
    except Exception:
        students_serialized = []

    # Formations for select
    try:
        formations_list = list(Formation.objects.all().values('id', 'nom'))
    except Exception:
        formations_list = []

    # Groupes: distinct groupe values from Inscription
    try:
        groupes_qs = Inscription.objects.values_list('groupe', flat=True).distinct()
        groupes_list = [g for g in groupes_qs if g]
    except Exception:
        groupes_list = []

    # Formateurs: prefer Enseignant then Utilisateur with statut
    try:
        formateurs_qs = Enseignant.objects.all()
        formateurs_list = [{'id': f.id, 'nom': str(f)} for f in formateurs_qs]
    except Exception:
        try:
            formateurs_qs = Utilisateur.objects.filter(statut__icontains='formateur')
            formateurs_list = [{'id': f.id, 'nom': f.prenom + ' ' + f.nom} for f in formateurs_qs]
        except Exception:
            formateurs_list = []

    context = {
        'reports': reports,
        # Provide the serialized students under the key 'students' so template's json_script works
    'students': students_serialized,
    'students_json': json.dumps(students_serialized, default=str, ensure_ascii=False),
        'formations': formations_list,
        'groupes': groupes_list,
        'formateurs': formateurs_list,
    }
    return render(request, 'rapports.html', context)


def paiements_debug(request):
    """Temporary debug endpoint that echoes request info for troubleshooting fetch/CSRF issues.
    Do not leave enabled in production.
    """
    try:
        info = {
            'method': request.method,
            'path': getattr(request, 'path', ''),
            'full_path': getattr(request, 'get_full_path', lambda: '')(),
            'META_excerpt': {k: request.META.get(k) for k in ['HTTP_HOST', 'HTTP_REFERER', 'HTTP_USER_AGENT', 'HTTP_COOKIE', 'CONTENT_TYPE', 'CONTENT_LENGTH', 'REMOTE_ADDR']},
            'post_keys': list(request.POST.keys()),
            'files_keys': list(request.FILES.keys()),
            'cookies': request.COOKIES,
            'body_length': len(request.body or b''),
            'body_preview': (request.body[:1000].decode(errors='replace') if request.body else ''),
        }
        return JsonResponse({'success': True, 'debug': info})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({'success': False, 'error': str(e)})


def etudiant_detail_json(request, pk):
    e = get_object_or_404(Etudiant, pk=pk)
    def _make_url(p):
        if not p:
            return ''
        try:
            url = default_storage.url(p)
        except Exception:
            url = (settings.MEDIA_URL + p) if getattr(settings, 'MEDIA_URL', None) else p
        # ensure it's an absolute-ish path the browser can fetch
        # normalize backslashes (Windows storage paths) -> web-friendly
        url = url.replace('\\', '/')
        if not url.startswith('http') and not url.startswith('/'):
            url = (settings.MEDIA_URL or '/') + url
        return url
    # build inscriptions details
    inscriptions = []
    for ins in e.inscriptions.select_related('formation').all():
        prix = None
        try:
            prix = Decimal(ins.prix_total) if getattr(ins, 'prix_total', None) is not None else None
        except Exception:
            prix = None
        # compute paid amount for this inscription
        try:
            paid_agg = Paiement.objects.filter(inscription_id=ins.id).aggregate(total=Sum('montant'))
            paid_amount = Decimal(paid_agg.get('total') or 0)
        except Exception:
            paid_amount = Decimal('0.00')
        # determine remaining for this inscription (if prix not set, try formation.prix_etudiant)
        ins_price = prix
        if ins_price is None:
            try:
                ins_price = Decimal(ins.formation.prix_etudiant) if getattr(ins, 'formation', None) and getattr(ins.formation, 'prix_etudiant', None) is not None else None
            except Exception:
                ins_price = None
        if ins_price is not None:
            try:
                remaining_ins = ins_price - paid_amount
                if remaining_ins < 0:
                    remaining_ins = Decimal('0.00')
            except Exception:
                remaining_ins = Decimal('0.00')
        else:
            remaining_ins = None
        inscriptions.append({
            'id': ins.id,
            'formation_id': ins.formation.id if ins.formation else None,
            'formation': ins.formation.nom if ins.formation else (getattr(ins, 'formation_text', '') or ''),
            'prix_total': str(prix) if prix is not None else None,
            'paid': str(paid_amount),
            'remaining': str(remaining_ins) if remaining_ins is not None else None,
            'date_inscription': ins.date_inscription.isoformat() if getattr(ins, 'date_inscription', None) else '',
        })

    # Use shared calculate_balances util to compute total_due, prev_paid and current remaining (ancien_solde)
    try:
        balances = calculate_balances(e.id)
        total_due = balances.get('total_due', 0.0)
        prev_paid = balances.get('prev_paid', 0.0)
        remaining = balances.get('ancien_solde', 0.0)
    except Exception:
        total_due = 0.0
        prev_paid = 0.0
        remaining = 0.0

    data = {
        'id': e.id,
        'nom': e.nom,
        'prenom': e.prenom,
        'sexe': e.sexe,
    'date_naissance': e.date_naissance.isoformat() if e.date_naissance else None,
    'date_inscription': e.date_inscription.isoformat() if getattr(e, 'date_inscription', None) else '',
        'lieu_naissance': e.lieu_naissance,
        'nationalite': e.nationalite,
        'nin': e.nin,
        'adresse': e.adresse,
        'telephone': e.telephone,
        'email': e.email,
        # list of formation names (legacy/simple view)
        'formations': [ins.get('formation') for ins in inscriptions],
        # structured inscriptions objects (used by client JS when present)
        'inscriptions': inscriptions,
        # keep the older key name for other templates that expect details
        'inscriptions_details': inscriptions,
        'total_due': str(total_due),
        'total_paid': str(prev_paid),
        'remaining_fees': str(remaining),
        'extrait_naissance_photo': e.extrait_naissance_photo,
        'carte_identite_photo': e.carte_identite_photo,
        # full URLs for templates/JS
        'extrait_naissance_photo_url': _make_url(e.extrait_naissance_photo),
        'carte_identite_photo_url': _make_url(e.carte_identite_photo),
        'photo_url': _make_url(getattr(e, 'photo', None)),
        'verification_step': e.verification_step,
    }
    # also include top-level 'inscriptions' for compatibility with older callers
    return JsonResponse({'success': True, 'etudiant': data, 'inscriptions': inscriptions})


from django.views.decorators.http import require_POST


@require_POST
def etudiant_upload_doc(request, pk):
    e = get_object_or_404(Etudiant, pk=pk)
    doc_type = request.POST.get('type')
    if doc_type not in ('extrait', 'carte'):
        return JsonResponse({'success': False, 'error': 'invalid type'}, status=400)
    f = request.FILES.get('file')
    if not f:
        return JsonResponse({'success': False, 'error': 'no file'}, status=400)
    try:
        filename = f"etudiants/{pk}_{doc_type}_{f.name}"
        path = default_storage.save(filename, ContentFile(f.read()))
        if doc_type == 'extrait':
            e.extrait_naissance_photo = path
        else:
            e.carte_identite_photo = path
        e.save()
        # return the publicly accessible URL too
        try:
            url = default_storage.url(path)
        except Exception:
            url = (settings.MEDIA_URL + path) if getattr(settings, 'MEDIA_URL', None) else path
        if not url.startswith('http') and not url.startswith('/'):
            url = (settings.MEDIA_URL or '/') + url
        return JsonResponse({'success': True, 'path': path, 'url': url})
    except Exception as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=500)


@require_POST
def etudiant_update_step(request, pk):
    e = get_object_or_404(Etudiant, pk=pk)
    try:
        step = int(request.POST.get('step', 0))
    except Exception:
        return JsonResponse({'success': False, 'error': 'invalid step'}, status=400)
    e.verification_step = step
    e.save()
    return JsonResponse({'success': True, 'step': e.verification_step})







def _unused_format_placeholder(request):
    # placeholder to avoid accidental top-level 'format' override and remove stray code
    return HttpResponse(status=204)


# ============================================
# STUDENT MOBILE VIEWS - Dynamic & Real Data
# ============================================

def student_dashboard(request):
    """Student dashboard with real data from database"""
    user_id = request.session.get('user_id')
    if not user_id:
        return redirect('login')
    
    try:
        user = Utilisateur.objects.get(id=user_id)
    except:
        return redirect('login')
    
    # Check if user is student
    if user.role != 'etudiant':
        return redirect('dashboard')
    
    # Get student profile
    try:
        etudiant = Etudiant.objects.get(email=user.email)
    except:
        etudiant = None
    
    # Calculate statistics
    inscriptions = Inscription.objects.filter(etudiant=etudiant) if etudiant else []
    paiements = Paiement.objects.filter(etudiant=etudiant) if etudiant else []
    
    # Stats
    total_formations = inscriptions.count()
    total_montant = paiements.aggregate(total=Sum('montant'))['total'] or 0
    total_paye = paiements.filter(statut='payé').aggregate(total=Sum('montant'))['total'] or 0
    total_reste = total_montant - total_paye
    
    context = {
        'user': user,
        'etudiant': etudiant,
        'total_formations': total_formations,
        'total_montant': total_montant,
        'total_paye': total_paye,
        'total_reste': total_reste,
        'prochain_event': None,  # Will be calculated if needed
    }
    
    return render(request, 'dashboard_etudiant.html', context)


def student_profile_edit(request):
    """Student profile completion form with real data"""
    user_id = request.session.get('user_id')
    if not user_id:
        return redirect('login')
    
    try:
        user = Utilisateur.objects.get(id=user_id)
    except:
        return redirect('login')
    
    if user.role != 'etudiant':
        return redirect('dashboard')
    
    # Get or create student profile - try different queries
    etudiant = None
    try:
        # Try by email first
        etudiant = Etudiant.objects.get(email=user.email)
    except:
        try:
            # Try by nom
            etudiant = Etudiant.objects.get(nom=user.nom)
        except:
            try:
                # Try by nom and prenom if available
                etudiant = Etudiant.objects.filter(nom__icontains=user.nom).first()
            except:
                pass
    
    if request.method == 'POST':
        if not etudiant:
            # Create new student
            etudiant = Etudiant()
            etudiant.email = user.email
        
        # Update personal info
        etudiant.nom = request.POST.get('nom', etudiant.nom if etudiant else user.nom)
        etudiant.prenom = request.POST.get('prenom', etudiant.prenom if etudiant else user.prenom)
        etudiant.email = user.email
        etudiant.telephone = request.POST.get('telephone', etudiant.telephone if etudiant else '')
        etudiant.mobile = request.POST.get('telephone', etudiant.mobile if etudiant else '')
        etudiant.adresse = request.POST.get('adresse', etudiant.adresse if etudiant else '')
        etudiant.situation_professionnelle = request.POST.get('situation', etudiant.situation_professionnelle if etudiant else '')
        
        # Handle date fields
        if request.POST.get('date_naissance'):
            etudiant.date_naissance = request.POST.get('date_naissance')
        if request.POST.get('lieu_naissance'):
            etudiant.lieu_naissance = request.POST.get('lieu_naissance')
        if request.POST.get('nin'):
            etudiant.nin = request.POST.get('nin')
        if request.POST.get('niveau_etude'):
            etudiant.niveau_etude = request.POST.get('niveau_etude')
        if request.POST.get('sexe'):
            etudiant.sexe = request.POST.get('sexe')
        
        # Set defaults if not already set
        if not etudiant.date_inscription:
            etudiant.date_inscription = date.today()
        if not etudiant.statut:
            etudiant.statut = 'inscrit'
        
        # Handle file uploads
        if 'birth_cert' in request.FILES:
            etudiant.extrait_naissance_photo = request.FILES['birth_cert']
        
        if 'id_card' in request.FILES:
            etudiant.carte_identite_photo = request.FILES['id_card']
        
        etudiant.save()
        
        return redirect('dashboard')
    
    # Calculate completion percentage
    completion = 0
    if etudiant:
        fields_filled = 0
        total_fields = 8
        if etudiant.nom: fields_filled += 1
        if etudiant.prenom: fields_filled += 1
        if etudiant.telephone or etudiant.mobile: fields_filled += 1
        if etudiant.adresse: fields_filled += 1
        if etudiant.situation_professionnelle: fields_filled += 1
        if etudiant.date_naissance: fields_filled += 1
        if etudiant.nin: fields_filled += 1
        if etudiant.extrait_naissance_photo or etudiant.carte_identite_photo: fields_filled += 1
        completion = int((fields_filled / total_fields) * 100)
    
    context = {
        'user': user,
        'etudiant': etudiant,
        'completion': completion,
    }
    
    return render(request, 'student_profile_edit.html', context)



def student_inscriptions(request):
    """View student enrollments with real data"""
    user_id = request.session.get('user_id')
    if not user_id:
        return redirect('login')
    
    try:
        user = Utilisateur.objects.get(id=user_id)
    except:
        return redirect('login')
    
    if user.role != 'etudiant':
        return redirect('dashboard')
    
    # Get student profile
    try:
        etudiant = Etudiant.objects.get(email=user.email)
    except:
        return redirect('student_profile_edit')
    
    # Get inscriptions
    inscriptions = Inscription.objects.filter(etudiant=etudiant).select_related('formation')
    
    # Filter by status
    status_filter = request.GET.get('status', 'all')
    if status_filter != 'all':
        if status_filter == 'inscrit':
            inscriptions = inscriptions.filter(statut='inscrit')
        elif status_filter == 'en_cours':
            inscriptions = inscriptions.filter(statut='en_cours')
        elif status_filter == 'terminee':
            inscriptions = inscriptions.filter(statut='terminée')
    
    # Add progress percentage
    for inscription in inscriptions:
        presences = Presence.objects.filter(inscription=inscription).count()
        total_sessions = 10  # Default, should come from formation
        inscription.progress = int((presences / total_sessions) * 100) if total_sessions > 0 else 0
    
    context = {
        'user': user,
        'etudiant': etudiant,
        'inscriptions': inscriptions,
        'status_filter': status_filter,
    }
    
    return render(request, 'student_inscriptions.html', context)


def student_payments(request):
    """View student payments with real data"""
    user_id = request.session.get('user_id')
    if not user_id:
        return redirect('login')
    
    try:
        user = Utilisateur.objects.get(id=user_id)
    except:
        return redirect('login')
    
    if user.role != 'etudiant':
        return redirect('dashboard')
    
    # Get student profile
    try:
        etudiant = Etudiant.objects.get(email=user.email)
    except:
        return redirect('student_profile_edit')
    
    # Get payments
    paiements = Paiement.objects.filter(etudiant=etudiant).order_by('-date_paiement')
    
    # Calculate totals
    total_montant = paiements.aggregate(total=Sum('montant'))['total'] or 0
    total_paye = paiements.filter(statut='payé').aggregate(total=Sum('montant'))['total'] or 0
    total_pending = paiements.filter(statut__in=['en attente', 'pending']).aggregate(total=Sum('montant'))['total'] or 0
    total_retard = paiements.filter(statut='en retard').aggregate(total=Sum('montant'))['total'] or 0
    
    # Filter by status
    status_filter = request.GET.get('status', 'all')
    if status_filter != 'all':
        if status_filter == 'paye':
            paiements = paiements.filter(statut='payé')
        elif status_filter == 'pending':
            paiements = paiements.filter(statut__in=['en attente', 'pending'])
        elif status_filter == 'retard':
            paiements = paiements.filter(statut='en retard')
    
    context = {
        'user': user,
        'etudiant': etudiant,
        'paiements': paiements,
        'total_montant': total_montant,
        'total_paye': total_paye,
        'total_pending': total_pending,
        'total_retard': total_retard,
        'status_filter': status_filter,
    }
    
    return render(request, 'student_payments.html', context)


def student_planning(request):
    """View student schedule/planning with real data"""
    user_id = request.session.get('user_id')
    if not user_id:
        return redirect('login')
    
    try:
        user = Utilisateur.objects.get(id=user_id)
    except:
        return redirect('login')
    
    if user.role != 'etudiant':
        return redirect('dashboard')
    
    # Get student profile
    try:
        etudiant = Etudiant.objects.get(email=user.email)
    except:
        return redirect('student_profile_edit')
    
    # Get calendar events for student's formations
    inscriptions = Inscription.objects.filter(etudiant=etudiant).values_list('formation_id', flat=True)
    events = CalendarEvent.objects.filter(formation_id__in=inscriptions).order_by('date_debut')
    
    # Group events by date
    from django.db.models import Q
    from datetime import datetime, timedelta
    
    today = date.today()
    upcoming_events = events.filter(date_debut__gte=today)
    past_events = events.filter(date_debut__lt=today)
    
    context = {
        'user': user,
        'etudiant': etudiant,
        'events': upcoming_events,
        'today': today,
    }
    
    return render(request, 'student_planning.html', context)


def student_inscriptions_detailed(request):
    """Detailed student enrollments page"""
    user_id = request.session.get('user_id')
    if not user_id:
        return redirect('login')
    
    try:
        user = Utilisateur.objects.get(id=user_id)
    except:
        return redirect('login')
    
    if user.role != 'etudiant':
        return redirect('dashboard')
    
    # Get student
    etudiant = None
    try:
        etudiant = Etudiant.objects.get(email=user.email)
    except:
        try:
            etudiant = Etudiant.objects.filter(nom=user.nom).first()
        except:
            pass
    
    if not etudiant:
        return redirect('student_profile_edit')
    
    # Get inscriptions with related formation
    inscriptions = Inscription.objects.filter(etudiant=etudiant).select_related('formation')
    
    # Filter by status
    status_filter = request.GET.get('status', 'all')
    if status_filter != 'all':
        inscriptions = inscriptions.filter(statut=status_filter)
    
    context = {
        'user': user,
        'etudiant': etudiant,
        'inscriptions': inscriptions,
        'status_filter': status_filter,
    }
    
    return render(request, 'student_inscriptions_detailed.html', context)


def student_payments_detailed(request):
    """Detailed student payments page with progress"""
    user_id = request.session.get('user_id')
    if not user_id:
        return redirect('login')
    
    try:
        user = Utilisateur.objects.get(id=user_id)
    except:
        return redirect('login')
    
    if user.role != 'etudiant':
        return redirect('dashboard')
    
    # Get student
    etudiant = None
    try:
        etudiant = Etudiant.objects.get(email=user.email)
    except:
        try:
            etudiant = Etudiant.objects.filter(nom=user.nom).first()
        except:
            pass
    
    if not etudiant:
        return redirect('student_profile_edit')
    
    # Get payments ordered by date
    paiements = Paiement.objects.filter(etudiant=etudiant).order_by('-date_paiement').select_related('inscription', 'formation')
    
    # Calculate totals
    total_montant = paiements.aggregate(total=Sum('montant'))['total'] or 0
    total_paye = paiements.filter(statut='payé').aggregate(total=Sum('montant'))['total'] or 0
    total_reste = total_montant - total_paye
    if total_reste < 0:
        total_reste = 0
    
    # Calculate percentage
    paiement_percent = int((total_paye / total_montant) * 100) if total_montant > 0 else 0
    
    context = {
        'user': user,
        'etudiant': etudiant,
        'paiements': paiements,
        'total_montant': total_montant,
        'total_paye': total_paye,
        'total_reste': total_reste,
        'paiement_percent': paiement_percent,
    }
    
    return render(request, 'student_payments_detailed.html', context)


def student_formations_available(request):
    """All available formations for students"""
    user_id = request.session.get('user_id')
    if not user_id:
        return redirect('login')
    
    try:
        user = Utilisateur.objects.get(id=user_id)
    except:
        return redirect('login')
    
    if user.role != 'etudiant':
        return redirect('dashboard')
    
    # Get all formations (exclude only if statut='inactif', show all others)
    formations = Formation.objects.exclude(statut='inactif').order_by('-date_creation')
    
    # Get unique categories/branches (exclude inactif)
    categories = Formation.objects.exclude(statut='inactif').values_list('branche', flat=True).distinct().order_by('branche')
    
    context = {
        'user': user,
        'formations': formations,
        'categories': [c for c in categories if c],  # Remove None values
    }
    
    return render(request, 'student_formations_available.html', context)


 







# ============================================
# MOBILE API - FINANCES & PAIEMENTS
# ============================================

from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse
from django.core.paginator import Paginator
from django.db.models import Sum, Q
from django.utils import timezone
import json
from datetime import datetime

# Models are already imported in views.py usually, but ensuring:
from .models import Paiement, Charge, ReglementEnseignants, SchoolVersement, Etudiant, Formation

@csrf_exempt
def api_finances_summary(request):
    """
    API Endpoint for Finances Summary (Mobile)
    """
    try:
        # 1. Totals
        paiements_total = Paiement.objects.aggregate(total=Sum('montant'))['total'] or 0
        reglements_enseignants_total = ReglementEnseignants.objects.aggregate(total=Sum('montant'))['total'] or 0
        try:
            charges_total = Charge.objects.aggregate(total=Sum('montant'))['total'] or 0
        except:
            charges_total = 0
            
        try:
            school_versements_total = SchoolVersement.objects.aggregate(total=Sum('montant'))['total'] or 0
        except:
            school_versements_total = 0

        # 2. Calculations
        tresorerie = (paiements_total + school_versements_total) - (charges_total + reglements_enseignants_total)
        benefice_net = paiements_total + school_versements_total
        
        # 3. Monthly Stats (Current Month)
        now = timezone.now()
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        
        paiements_month = Paiement.objects.filter(date_paiement__gte=month_start.date()).aggregate(total=Sum('montant'))['total'] or 0
        charges_month = Charge.objects.filter(date_paiement__gte=month_start.date()).aggregate(total=Sum('montant'))['total'] or 0
        
        # 4. Top Formations (Revenue)
        top_formations = []
        try:
            top_qs = list(Paiement.objects.values('formation__nom').annotate(total=Sum('montant')).order_by('-total')[:5])
            total_rev = sum([x.get('total') or 0 for x in top_qs])
            if total_rev > 0:
                for item in top_qs:
                    top_formations.append({
                        'label': item.get('formation__nom') or 'Autre',
                        'value': float(item.get('total') or 0),
                        'percent': round((float(item.get('total') or 0) / float(paiements_total)) * 100, 1) if paiements_total else 0
                    })
        except:
            pass

        data = {
            'success': True,
            'stats': {
                'solde_actuel': float(tresorerie),
                'total_entrees': float(paiements_total + school_versements_total),
                'total_sorties': float(charges_total + reglements_enseignants_total),
                'charges_total': float(charges_total),
                'benefice_net': float(benefice_net),
            },
            'month_stats': {
                'entrees': float(paiements_month),
                'sorties': float(charges_month),
            },
            'top_formations': top_formations
        }
        return JsonResponse(data)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)

@csrf_exempt
def api_paiements_list(request):
    """
    API for listing payments with pagination and search
    """
    try:
        page = int(request.GET.get('page', 1))
        limit = int(request.GET.get('limit', 20))
        search = request.GET.get('q', '').lower()
        
        qs = Paiement.objects.all().select_related('etudiant', 'formation').order_by('-date_paiement')
        
        if search:
            qs = qs.filter(
                Q(reference__icontains=search) | 
                Q(etudiant__nom__icontains=search) |
                Q(etudiant__prenom__icontains=search)
            )
            
        total_count = qs.count()
        paginator = Paginator(qs, limit)
        current_page = paginator.get_page(page)
        
        paiements = []
        for p in current_page:
            paiements.append({
                'id': p.id,
                'reference': p.reference,
                'etudiant_nom': f"{p.etudiant.prenom} {p.etudiant.nom}" if p.etudiant else "Inconnu",
                'formation': p.formation.nom if p.formation else (p.ecole or "-"),
                'montant': float(p.montant),
                'date_paiement': p.date_paiement.isoformat() if p.date_paiement else None,
                'mode_paiement': p.mode_paiement,
                'remarques': p.remarques
            })
            
        return JsonResponse({
            'success': True,
            'paiements': paiements,
            'total': total_count,
            'pages': paginator.num_pages,
            'current_page': page
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)

@csrf_exempt
def api_etudiants_simple(request):
    """
    Return simple list of students for dropdown
    """
    try:
        search = request.GET.get('q', '')
        qs = Etudiant.objects.all().values('id', 'nom', 'prenom', 'formation__nom', 'groupe__nom').order_by('nom')[:50]
        
        if search:
            qs = Etudiant.objects.filter(
                Q(nom__icontains=search) | Q(prenom__icontains=search)
            ).values('id', 'nom', 'prenom', 'formation__nom', 'groupe__nom')[:50]
            
        etudiants = list(qs)
        return JsonResponse({'success': True, 'etudiants': etudiants})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)

@csrf_exempt
def api_paiement_create(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)
        
    try:
        data = json.loads(request.body)
        
        # Basic validation
        if not data.get('etudiant_id') or not data.get('montant'):
            return JsonResponse({'success': False, 'error': 'Etudiant et montant requis'}, status=400)
            
        etudiant = Etudiant.objects.get(id=data['etudiant_id'])
        
        paiement = Paiement(
            etudiant=etudiant,
            montant=data['montant'],
            date_paiement=data.get('date_paiement') or timezone.now().date(),
            mode_paiement=data.get('mode_paiement', 'EspÃ¨ces'),
            remarques=data.get('remarques', ''),
            reference=data.get('reference') or f"PAY-{int(datetime.now().timestamp())}",
            formation=etudiant.formation # Auto-link formation
        )
        paiement.save()
        
        return JsonResponse({
            'success': True,
            'paiement': {
                'id': paiement.id,
                'reference': paiement.reference,
                'montant': float(paiement.montant),
                'etudiant_nom': str(etudiant),
                'date_paiement': paiement.date_paiement.isoformat()
            }
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)
@csrf_exempt
def api_charges_list(request):
    try:
        page = int(request.GET.get('page', 1))
        limit = int(request.GET.get('limit', 20))
        search = request.GET.get('q', '').lower()
        
        qs = Charge.objects.all().order_by('-date_paiement')
        if search:
            qs = qs.filter(Q(type_charge__icontains=search) | Q(reference__icontains=search))
            
        total_count = qs.count()
        paginator = Paginator(qs, limit)
        current_page = paginator.get_page(page)
        
        charges = []
        for c in current_page:
            # Check for attachment
            attachment_url = None
            if getattr(c, 'attachment', None):
                attachment_url = request.build_absolute_uri(c.attachment.url)
            elif getattr(c, 'document', None): # Fallback to old field if any
                attachment_url = request.build_absolute_uri(f"/media/{c.document}")

            charges.append({
                'id': c.id,
                'type_charge': c.type_charge,
                'montant': float(c.montant),
                'date_paiement': c.date_paiement.isoformat() if c.date_paiement else None,
                'reference': c.reference,
                'remarque': c.remarque,
                'contact': c.contact or '',
                'fournisseur': c.fournisseur or '',
                'nom_contact': c.nom_contact or '',
                'attachment': attachment_url
            })
            
        # Compute stats for the dashboard
        now = timezone.now()
        total_all = Charge.objects.aggregate(total=Sum('montant'))['total'] or 0
        month_total = Charge.objects.filter(
            date_paiement__year=now.year,
            date_paiement__month=now.month
        ).aggregate(total=Sum('montant'))['total'] or 0
        
        # Day total (today)
        day_total = Charge.objects.filter(
            date_paiement=now.date()
        ).aggregate(total=Sum('montant'))['total'] or 0
        
        # Type distribution for chart/categories
        type_stats = Charge.objects.values('type_charge').annotate(
            total=Sum('montant'),
            count=Count('id')
        ).order_by('-total')[:15]
            
        return JsonResponse({
            'success': True, 
            'charges': charges, 
            'total': total_count,
            'stats': {
                'total_all': float(total_all),
                'month_total': float(month_total),
                'day_total': float(day_total),
                'count': total_count,
                'type_distribution': list(type_stats)
            }
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)

@csrf_exempt
def api_charge_create(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)
    try:
        # Handle both JSON and multipart/form-data
        if request.content_type == 'application/json':
            data = json.loads(request.body)
        else:
            data = request.POST

        charge = Charge(
            type_charge=data.get('type_charge'),
            montant=data.get('montant'),
            date_paiement=data.get('date_paiement') or timezone.now().date(),
            reference=data.get('reference'),
            remarque=data.get('remarque'),
            contact=data.get('contact'),
            fournisseur=data.get('fournisseur') or None,
            nom_contact=data.get('nom_contact') or None
        )
        
        # Handle file upload
        if 'attachment' in request.FILES:
            charge.attachment = request.FILES['attachment']
        elif data.get('attachment_url'):
            # Handle already uploaded file (from temp upload)
            import os
            import shutil
            from django.conf import settings
            
            attachment_url = data.get('attachment_url')
            # Extract filename from URL
            if '/temp_uploads/' in attachment_url:
                filename = attachment_url.split('/temp_uploads/')[-1].split('?')[0]
                temp_path = os.path.join(settings.MEDIA_ROOT, 'temp_uploads', filename)
                
                if os.path.exists(temp_path):
                    # Move file to charges directory
                    charges_dir = os.path.join(settings.MEDIA_ROOT, 'charges')
                    os.makedirs(charges_dir, exist_ok=True)
                    
                    new_filename = f"charge_{timezone.now().strftime('%Y%m%d%H%M%S')}_{filename.split('_', 2)[-1]}"
                    new_path = os.path.join(charges_dir, new_filename)
                    shutil.move(temp_path, new_path)
                    
                    charge.attachment = f"charges/{new_filename}"
            
        charge.save()
        
        attachment_url = None
        if charge.attachment:
            attachment_url = request.build_absolute_uri(charge.attachment.url)

        return JsonResponse({
            'success': True, 
            'id': charge.id,
            'attachment': attachment_url
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)

@csrf_exempt
def api_upload_temp(request):
    """Temporary upload endpoint for immediate file upload with progress"""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)
    try:
        if 'attachment' not in request.FILES:
            return JsonResponse({'success': False, 'error': 'No file provided'}, status=400)
        
        uploaded_file = request.FILES['attachment']
        
        # Save to a temporary location (or could save to TempUpload model)
        import os
        import uuid
        from django.conf import settings
        
        # Generate unique filename
        ext = os.path.splitext(uploaded_file.name)[1]
        unique_name = f"temp_{uuid.uuid4().hex}{ext}"
        
        # Ensure temp directory exists
        temp_dir = os.path.join(settings.MEDIA_ROOT, 'temp_uploads')
        os.makedirs(temp_dir, exist_ok=True)
        
        # Save file
        file_path = os.path.join(temp_dir, unique_name)
        with open(file_path, 'wb+') as destination:
            for chunk in uploaded_file.chunks():
                destination.write(chunk)
        
        # Return URL
        attachment_url = request.build_absolute_uri(f'/media/temp_uploads/{unique_name}')
        
        return JsonResponse({
            'success': True,
            'filename': unique_name,
            'original_name': uploaded_file.name,
            'attachment': attachment_url,
            'size': uploaded_file.size
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)

@csrf_exempt
def api_charge_detail(request, pk):
    try:
        c = get_object_or_404(Charge, pk=pk)
        attachment_url = None
        if getattr(c, 'attachment', None):
            attachment_url = request.build_absolute_uri(c.attachment.url)
        return JsonResponse({
            'success': True,
            'charge': {
                'id': c.id,
                'type_charge': c.type_charge or '',
                'montant': float(c.montant or 0),
                'date_paiement': c.date_paiement.isoformat() if c.date_paiement else None,
                'mode_paiement': c.mode_paiement or '',
                'reference': c.reference or '',
                'remarque': c.remarque or '',
                'formation_id': c.formation_id,
                'formation_nom': c.formation.nom if c.formation else None,
                'contact': c.contact or '',
                'attachment': attachment_url,
            }
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)

@csrf_exempt
@require_http_methods(["POST"])
def api_charge_update(request, pk):
    """API endpoint to update an existing charge."""
    try:
        charge = get_object_or_404(Charge, pk=pk)
        
        # Parse request data
        if request.content_type and 'application/json' in request.content_type:
            try:
                data = json.loads(request.body.decode('utf-8') or '{}')
            except Exception:
                data = {}
        else:
            data = request.POST.dict()

        # Update fields if provided
        if 'type_charge' in data and data['type_charge']:
            charge.type_charge = data['type_charge'].strip()
        
        if 'montant' in data:
            try:
                charge.montant = Decimal(str(data['montant']))
            except Exception:
                pass
        
        if 'date_paiement' in data and data['date_paiement']:
            try:
                charge.date_paiement = datetime.strptime(data['date_paiement'], '%Y-%m-%d').date()
            except Exception:
                pass
        
        if 'mode_paiement' in data:
            charge.mode_paiement = data['mode_paiement'] or None
        
        if 'reference' in data:
            charge.reference = data['reference'] or None
        
        if 'remarque' in data:
            charge.remarque = data['remarque'] or None
            
        if 'contact' in data:
            charge.contact = data['contact'] or None
        
        if 'formation_id' in data:
            charge.formation_id = data['formation_id'] if data['formation_id'] else None
        
        # Handle attachment from FILES if any
        if request.FILES.get('attachment'):
            charge.attachment = request.FILES['attachment']

        charge.save()
        
        attachment_url = None
        if getattr(charge, 'attachment', None):
            # Return relative URL so frontend can prepend SERVER_URL
            attachment_url = request.build_absolute_uri(charge.attachment.url)
            
        charge_data = {
            'id': charge.id,
            'type_charge': charge.type_charge or '',
            'montant': float(charge.montant or 0),
            'date_paiement': charge.date_paiement.isoformat() if charge.date_paiement else None,
            'mode_paiement': charge.mode_paiement or '',
            'reference': charge.reference or '',
            'remarque': charge.remarque or '',
            'formation_id': charge.formation_id,
            'formation_nom': charge.formation.nom if charge.formation else None,
            'contact': charge.contact or '',
            'attachment': attachment_url,
        }
        
        return JsonResponse({'success': True, 'charge': charge_data})

    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)

@csrf_exempt

def api_charges_stats(request):
    try:
        total = Charge.objects.aggregate(total=Sum('montant'))['total'] or 0
        now = timezone.now()
        month_start = now.replace(day=1)
        month_total = Charge.objects.filter(date_paiement__gte=month_start.date()).aggregate(total=Sum('montant'))['total'] or 0
        return JsonResponse({'success': True, 'total': float(total), 'month_total': float(month_total)})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)

@csrf_exempt
def api_charges_types(request):
    """API endpoint: GET available charge types with sub-types."""
    try:
        main_types = [
            {'key': 'travaux', 'label': 'Travaux et entretien'},
            {'key': 'amenagement', 'label': 'Aménagement'},
            {'key': 'vehicule', 'label': 'Dépenses Véhicule'},
            {'key': 'fiscales', 'label': 'Charges Fiscales'},
            {'key': 'fonctionnement', 'label': 'Charge de fonctionnement'},
            {'key': 'autres', 'label': 'Autres frais'},
            {'key': 'abonnement', 'label': 'Charge abonnement'},
        ]
        
        sub_types = {
            'travaux': [
                {'key': 'maconnerie', 'label': 'Maçonnerie'},
                {'key': 'plomberie', 'label': 'Plomberie'},
                {'key': 'carrelage', 'label': 'Carrelage et faïence'},
                {'key': 'peinture', 'label': 'Peinture'},
                {'key': 'courant_fort', 'label': 'Courant fort'},
                {'key': 'courant_faible', 'label': 'Courant faible'},
                {'key': 'menuiserie', 'label': 'Menuiserie générale'},
                {'key': 'finitions', 'label': 'Finitions'},
                {'key': 'soudeur', 'label': 'Soudeur'},
                {'key': 'quincaillerie', 'label': 'Quincaillerie'},
            ],
            'amenagement': [
                {'key': 'mobilier', 'label': 'Mobilier'},
                {'key': 'materiel_equipement', 'label': 'Matériel et équipement'},
            ],
            'vehicule': [
                {'key': 'carburant', 'label': 'Carburant'},
                {'key': 'assurance', 'label': 'Assurance'},
                {'key': 'entretien_mecanique', 'label': 'Entretien et mécanique'},
            ],
            'fiscales': [
                {'key': 'impot', 'label': 'Impôt'},
                {'key': 'cnas', 'label': 'CNAS'},
                {'key': 'casnos', 'label': 'CASNOS'},
                {'key': 'quittance_fiscale', 'label': 'Quittance fiscale'},
            ],
            'fonctionnement': [
                {'key': 'fourniture_bureautique', 'label': 'Fourniture bureautique'},
                {'key': 'impression_photocopie', 'label': 'Impression et photocopie'},
                {'key': 'publicite', 'label': 'Publicité'},
                {'key': 'loyer', 'label': 'Loyer'},
                {'key': 'entretien_nettoyage', 'label': 'Entretien et nettoyage'},
            ],
            'autres': [
                {'key': 'alimentation', 'label': 'Alimentations'},
                {'key': 'cafe_restaurant', 'label': 'Café & Restaurant'},
                {'key': 'voyage', 'label': 'Voyage'},
                {'key': 'transport', 'label': 'Transport'},
                {'key': 'divers', 'label': 'Divers'},
            ],
            'abonnement': [
                {'key': 'facture_electricite', 'label': "Facture d'électricité"},
                {'key': 'facture_gaz', 'label': 'Facture Gaz'},
                {'key': 'facture_internet', 'label': 'Facture internet'},
                {'key': 'facture_telephone', 'label': 'Facture téléphone'},
                {'key': 'facture_TV', 'label': 'Facture TV'},
            ],
        }
        
        return JsonResponse({
            'success': True,
            'main_types': main_types,
            'sub_types': sub_types,
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)

@csrf_exempt
def api_paiement_update(request, pk):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)
    try:
        data = json.loads(request.body)
        paiement = get_object_or_404(Paiement, pk=pk)
        if data.get('etudiant_id'):
            paiement.etudiant = get_object_or_404(Etudiant, pk=data['etudiant_id'])
        paiement.montant = data.get('montant', paiement.montant)
        paiement.date_paiement = data.get('date_paiement', paiement.date_paiement)
        paiement.mode_paiement = data.get('mode_paiement', paiement.mode_paiement)
        paiement.remarques = data.get('remarques', paiement.remarques)
        paiement.save()
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)

@csrf_exempt
def api_paiement_delete(request, pk):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)
    try:
        paiement = get_object_or_404(Paiement, pk=pk)
        paiement.delete()
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@csrf_exempt
def api_mobile_ecoles_list(request):
    """
    API Version of ecole_paiements_view logic.
    Returns list of external schools with financial summary.
    """
    try:
        from collections import defaultdict
        ecoles = defaultdict(float)
        ecoles_counts = defaultdict(int)
        
        # 1. Calculate Theoretical Shares
        ins_qs = Inscription.objects.select_related('formation').filter(ecole__isnull=False).exclude(ecole__exact='').all()
        for ins in ins_qs:
            try:
                ecole_name = (ins.ecole or '').strip()
                if not ecole_name: continue
                if 'genie' in ecole_name.lower() and 'system' in ecole_name.lower(): continue
                
                base = 0.0
                if getattr(ins, 'prix_total', None) is not None:
                    base = float(ins.prix_total or 0)
                elif getattr(ins, 'formation', None) and getattr(ins.formation, 'prix_etudiant', None) is not None:
                    base = float(ins.formation.prix_etudiant or 0)

                fname = (getattr(ins.formation, 'nom', '') or '').lower() if getattr(ins, 'formation', None) else ''
                deducted = 0.0
                if 'fibre' in fname:
                    if ecole_name.lower() == 'jura school': deducted = 4500.0
                    else: deducted = 3000.0
                elif 'reseau' in fname or 'camera' in fname or 'caméra' in fname:
                    deducted = 2000.0

                remaining = max(0.0, base - deducted)
                school_share = (remaining / 2.0) + deducted
                ecoles[ecole_name] += school_share
                ecoles_counts[ecole_name] += 1
            except Exception:
                continue

        # 2. Subtract Payments (Paiement + SchoolVersement)
        ecoles_summary = dict(ecoles)
        final_list = []
        
        for ename in list(ecoles_summary.keys()):
            try:
                paid_paiements = Paiement.objects.filter(ecole__iexact=ename).aggregate(total=Sum('montant'))['total'] or 0
                paid_school = 0
                try:
                    paid_school = SchoolVersement.objects.filter(ecole__iexact=ename).aggregate(total=Sum('montant'))['total'] or 0
                except: pass
                
                paid_total = (paid_paiements or 0) + (paid_school or 0)
                remaining_due = max(0.0, ecoles_summary.get(ename, 0.0) - float(paid_total))
                
                final_list.append({
                    'name': ename,
                    'count': ecoles_counts.get(ename, 0),
                    'total_share': float(format(ecoles_summary.get(ename, 0), '.2f')),
                    'paid': float(format(paid_total, '.2f')),
                    'remaining': float(format(remaining_due, '.2f'))
                })
            except: continue
        
        # Sort by remaining desc
        final_list.sort(key=lambda x: x['remaining'], reverse=True)
            
        return JsonResponse({'success': True, 'ecoles': final_list})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)

@csrf_exempt
def api_mobile_ecole_detail(request):
    """
    Get students and history for a specific school. 
    Query: ?name=SchoolName
    """
    try:
        selected = request.GET.get('name')
        if not selected: return JsonResponse({'success': False, 'error': 'Missing name param'}, status=400)
        
        students = []
        sel_qs = Inscription.objects.select_related('formation', 'etudiant').filter(ecole__iexact=selected)
        
        for ins in sel_qs:
            try:
                base = 0.0
                if getattr(ins, 'prix_total', None) is not None:
                    base = float(ins.prix_total or 0)
                elif getattr(ins, 'formation', None) and getattr(ins.formation, 'prix_etudiant', None) is not None:
                    base = float(ins.formation.prix_etudiant or 0)
                    
                fname = (getattr(ins.formation, 'nom', '') or '').lower() if getattr(ins, 'formation', None) else ''
                deducted = 0.0
                if 'fibre' in fname:
                    if selected.lower() == 'jura school': deducted = 4500.0
                    else: deducted = 3000.0
                elif 'reseau' in fname or 'camera' in fname or 'caméra' in fname:
                    deducted = 2000.0
                    
                remaining = max(0.0, base - deducted)
                share = (remaining / 2.0) + deducted
                
                students.append({
                    'id': ins.id,
                    'name': f"{getattr(ins.etudiant,'prenom','')} {getattr(ins.etudiant,'nom','')}",
                    'formation': getattr(ins.formation, 'nom', ''),
                    'share': round(share, 2)
                })
            except: continue
            
        # Versements history
        history = []
        try:
            svs = SchoolVersement.objects.filter(ecole__iexact=selected).order_by('-date_versement')[:20]
            for s in svs:
                history.append({
                    'id': s.id,
                    'type': 'versement',
                    'date': s.date_versement.isoformat(),
                    'amount': float(s.montant),
                    'note': s.note or ''
                })
        except: pass
        
        return JsonResponse({'success': True, 'students': students, 'history': history})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)
