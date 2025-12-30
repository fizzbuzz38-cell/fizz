from django.db import models
from django.utils import timezone
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.conf import settings
from django.db.models import Sum, Q
from .utils import calculate_balances




class Utilisateur(models.Model):
	nom = models.CharField(max_length=100)
	prenom = models.CharField(max_length=100)
	email = models.EmailField(max_length=150, unique=True)
	mot_de_passe = models.CharField(max_length=255)
	statut = models.CharField(max_length=50, null=True, blank=True)
	photo = models.CharField(max_length=255, null=True, blank=True)
	etat_compte = models.CharField(max_length=50, null=True, blank=True)
	date_creation = models.DateTimeField(null=True, blank=True)
	derniere_connexion = models.DateTimeField(null=True, blank=True)
	code_utilisateur = models.CharField(max_length=50, null=True, blank=True, unique=True)
	role = models.CharField(max_length=50, null=True, blank=True) 

	class Meta:
		db_table = 'utilisateur'

	def __str__(self):
		return f"{self.prenom} {self.nom} <{self.email}>"


class Enseignant(models.Model):
	matricule = models.CharField(max_length=64, unique=True, null=True, blank=True)
	nom = models.CharField(max_length=150)
	prenom = models.CharField(max_length=150)
	email = models.EmailField(max_length=255, null=True, blank=True)
	telephone = models.CharField(max_length=50, null=True, blank=True)
	adresse = models.TextField(null=True, blank=True)
	date_naissance = models.DateField(null=True, blank=True)
	lieu_naissance = models.CharField(max_length=255, null=True, blank=True)
	SEXE_CHOICES = (
		('M', 'Masculin'),
		('F', 'Feminin'),
		('NB', 'Non binaire'),
	)
	sexe = models.CharField(max_length=3, choices=SEXE_CHOICES, null=True, blank=True)
	photo = models.CharField(max_length=255, null=True, blank=True)
	niveau = models.CharField(max_length=100, null=True, blank=True)
	specialite = models.CharField(max_length=255, null=True, blank=True)
	bio = models.TextField(null=True, blank=True)
	date_embauche = models.DateField(null=True, blank=True)
	salaire = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
	statut = models.CharField(max_length=50, default='actif')
	formation = models.ForeignKey('Formation', on_delete=models.SET_NULL, db_column='formation_id', null=True, blank=True, related_name='enseignants')
	is_active = models.BooleanField(default=True)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)
	

	class Meta:
		db_table = 'enseignant'

	def __str__(self):
		return f"{self.prenom} {self.nom}"


class Formation(models.Model):
	nom = models.CharField(max_length=150)
	# Full rich/content body for the formation (matches SQL `contenu` column)
	contenu = models.TextField(null=True, blank=True)
	photo = models.CharField(max_length=255, null=True, blank=True)
	# optional PDF support file (stored relative to MEDIA_ROOT)
	pdf = models.CharField(max_length=255, null=True, blank=True)
	prix_etudiant = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
	prix_fonctionnaire = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
	duree = models.CharField(max_length=50, null=True, blank=True)
	branche = models.CharField(max_length=100, null=True, blank=True)
	categorie = models.CharField(max_length=100, null=True, blank=True)
	niveau = models.CharField(max_length=50, null=True, blank=True)
	date_creation = models.DateTimeField(null=True, blank=True)
	statut = models.CharField(max_length=20, null=True, blank=True)

	class Meta:
		db_table = 'formation'

	def __str__(self):
		return self.nom


class Salle(models.Model):
	nom = models.CharField(max_length=100)
	capacite = models.IntegerField(null=True, blank=True)
	equipements = models.TextField(null=True, blank=True)
	statut = models.CharField(max_length=20, null=True, blank=True)

	class Meta:
		db_table = 'salle'

	def __str__(self):
		return self.nom






class Module(models.Model):
	formation = models.ForeignKey(Formation, on_delete=models.CASCADE, db_column='formation_id', related_name='modules')
	titre = models.CharField(max_length=255)
	description = models.TextField(null=True, blank=True)
	ordre = models.IntegerField(default=0)
	duree = models.CharField(max_length=50, null=True, blank=True)
	created_at = models.DateTimeField(null=True, blank=True)
	updated_at = models.DateTimeField(null=True, blank=True)

	class Meta:
		db_table = 'module'

	def __str__(self):
		return f"{self.titre} ({self.formation})"


class Etudiant(models.Model):
	nom = models.CharField(max_length=100)
	prenom = models.CharField(max_length=100)
	nom_arabe = models.CharField(max_length=100, null=True, blank=True)
	prenom_arabe = models.CharField(max_length=100, null=True, blank=True)
	sexe = models.CharField(max_length=10, null=True, blank=True)
	date_naissance = models.DateField(null=True, blank=True)
	lieu_naissance = models.CharField(max_length=100, null=True, blank=True)
	nationalite = models.CharField(max_length=50, null=True, blank=True)
	nin = models.CharField(max_length=50, null=True, blank=True)
	adresse = models.TextField(null=True, blank=True)
	telephone = models.CharField(max_length=20, null=True, blank=True)
	mobile = models.CharField(max_length=20, null=True, blank=True)
	email = models.EmailField(max_length=150, null=True, blank=True)
	niveau_etude = models.CharField(max_length=100, null=True, blank=True)
	dernier_diplome = models.CharField(max_length=100, null=True, blank=True)
	situation_professionnelle = models.CharField(max_length=100, null=True, blank=True)
	photo = models.CharField(max_length=255, null=True, blank=True)
	date_inscription = models.DateField(null=True, blank=True)
	formation = models.ForeignKey(Formation, on_delete=models.SET_NULL, db_column='formation_id', null=True, blank=True, related_name='etudiants')
	duree_formation = models.CharField(max_length=50, null=True, blank=True)
	# Free-text formation name when the FK is not used
	formation_text = models.CharField(max_length=255, null=True, blank=True)
	salle = models.CharField(max_length=50, null=True, blank=True)
	statut = models.CharField(max_length=50, null=True, blank=True)
	remarques = models.TextField(null=True, blank=True)
	# documents: paths to stored files (relative to MEDIA_ROOT)
	extrait_naissance_photo = models.CharField(max_length=255, null=True, blank=True)
	carte_identite_photo = models.CharField(max_length=255, null=True, blank=True)
	# verification progress: 0=info,1=documents,2=email,3=verifié
	verification_step = models.IntegerField(default=0)
	
	groupe = models.ForeignKey(
		'Groupe',
		on_delete=models.SET_NULL,
		db_column='groupe_id',
		null=True,
		blank=True,
		related_name='etudiants'
	)

	class Meta:

		
		db_table = 'etudiant'

	def __str__(self):
		return f"{self.prenom} {self.nom}"


class Inscription(models.Model):
	etudiant = models.ForeignKey(Etudiant, on_delete=models.CASCADE, db_column='etudiant_id', related_name='inscriptions')
	formation = models.ForeignKey(Formation, on_delete=models.CASCADE, db_column='formation_id', related_name='inscriptions')
	date_inscription = models.DateTimeField(default=timezone.now)
	statut = models.CharField(max_length=50, default='inscrit')
	progress_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)
	prix_total = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
	remarques = models.TextField(null=True, blank=True)
	# new columns added to DB: session ('estivale'|'normale') and groupe (e.g. 'GR1', 'GR2', 'GR3')
	session = models.CharField(max_length=50, null=True, blank=True)
	groupe = models.ForeignKey(
		'Groupe',
		on_delete=models.SET_NULL,
		db_column='groupe_id',
		null=True,
		blank=True,
		related_name='inscriptions'
	)
	ecole = models.CharField(max_length=255, null=True, blank=True)

	class Meta:
		db_table = 'inscription'
		unique_together = (('etudiant', 'formation'),)

	def __str__(self):
		return f"{self.etudiant} -> {self.formation} ({self.groupe.nom if self.groupe else 'Sans groupe'})"


class Paiement(models.Model):
	etudiant = models.ForeignKey(Etudiant, on_delete=models.CASCADE, db_column='etudiant_id', related_name='paiements')
	formation = models.ForeignKey(Formation, on_delete=models.SET_NULL, db_column='formation_id', related_name='paiements', null=True, blank=True)
	# optional link to the inscription (if payment is for a specific inscription)
	inscription = models.ForeignKey('Inscription', on_delete=models.SET_NULL, db_column='inscription_id', null=True, blank=True, related_name='paiements')
	montant = models.DecimalField(max_digits=10, decimal_places=2)
	date_paiement = models.DateField() 
	# Ecole associée au paiement (copie depuis l'inscription si présente)
	ecole = models.CharField(max_length=255, null=True, blank=True)
	mode_paiement = models.CharField(max_length=50, null=True, blank=True)
	numero_cheque = models.CharField(max_length=50, null=True, blank=True)
	date_cheque = models.DateField(null=True, blank=True)
	compte_bancaire = models.CharField(max_length=100, null=True, blank=True)
	statut = models.CharField(max_length=20, null=True, blank=True)# unique reference like RET000123
	reference = models.CharField(max_length=100, null=True, blank=True, unique=True)# snapshot of the student's remaining balance after this payment (useful for reports)
	balance_after = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
	remarques = models.TextField(null=True, blank=True)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	class Meta:
		db_table = 'paiement'

	def __str__(self):
		ref = self.reference or f"#{self.id}"
		return f"{ref} {self.montant} - {self.etudiant}"


class SchoolVersement(models.Model):
	"""Represent a single external-school versement (not linked to a student).

	This model is used to record a school-level payment so we do not need to
	create a Paiement per inscription/student. It stores a batch_id to
	correlate with older versement batches and a note. The view logic will
	subtract these amounts from the per-school summary when displaying
	remaining due.
	"""
	ecole = models.CharField(max_length=255, null=True, blank=True)
	montant = models.DecimalField(max_digits=12, decimal_places=2)
	date_versement = models.DateField()
	batch_id = models.CharField(max_length=64, null=True, blank=True, unique=True)
	note = models.TextField(null=True, blank=True)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	class Meta:
		db_table = 'school_versement'

	def __str__(self):
		return f"SV{self.batch_id} {self.montant} - {self.ecole}"


# Ensure a stable, human-friendly reference is set after a payment is created
# and compute a snapshot of the student's remaining balance after this payment.
# Assumptions:
# - Reference format: "RET" followed by the payment's numeric id (e.g. RET123).
# - balance_after is computed using the sum of all inscriptions.prix_total for the student
#   minus the cumulative paid amount up to and including this payment.
@receiver(post_save, sender=Paiement)
def paiement_post_save(sender, instance, created, **kwargs):
	# If ecole missing on Paiement but payment linked to an Inscription, copy it
	try:
		if (not instance.ecole or instance.ecole == '') and instance.inscription:
			ins = instance.inscription
			if ins and getattr(ins, 'ecole', None):
				sender.objects.filter(pk=instance.pk).update(ecole=ins.ecole)
	except Exception:
		pass
	# If reference missing, set it to RET{pk}
	if not instance.reference:
		# Use update to avoid recursive signal loop via instance.save()
		sender.objects.filter(pk=instance.pk).update(reference=f"RET{instance.pk}")

	# Compute and persist balance_after if possible
	try:
		etu = instance.etudiant
		if etu:
			# use shared util to compute balances (ordering-aware)
			res = calculate_balances(etu.id, payment_date=instance.date_paiement, montant=instance.montant, exclude_payment_pk=instance.pk)
			apres_solde = res.get('apres_solde')
			# persist balance_after
			if instance.balance_after is None or float(instance.balance_after) != apres_solde:
				sender.objects.filter(pk=instance.pk).update(balance_after=apres_solde)
			# update statut: 'Réglé' when remaining balance is zero
			statut = 'Réglé' if (apres_solde == 0 or apres_solde == 0.0) else 'Non réglé'
			if instance.statut != statut:
				sender.objects.filter(pk=instance.pk).update(statut=statut)
	except Exception:
		# avoid breaking on unexpected data; best-effort only
		pass


class BankReconciliation(models.Model):
	paiement = models.ForeignKey(Paiement, on_delete=models.SET_NULL, db_column='paiement_id', null=True, blank=True, related_name='reconciliations')
	date_import = models.DateTimeField(null=True, blank=True)
	bank_ref = models.CharField(max_length=255, null=True, blank=True)
	amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
	matched = models.BooleanField(default=False)
	notes = models.TextField(null=True, blank=True)

	class Meta:
		db_table = 'bank_reconciliation'

	def __str__(self):
		return f"BankReconciliation {self.bank_ref} - {self.amount}"


class ReglementEnseignants(models.Model):
	enseignant = models.ForeignKey(Enseignant, on_delete=models.CASCADE, db_column='enseignant_id', related_name='reglements')
	montant = models.DecimalField(max_digits=10, decimal_places=2)
	balance_after = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
	date_reglement = models.DateField()
	mode_reglement = models.CharField(max_length=50, null=True, blank=True)
	statut = models.CharField(max_length=20, null=True, blank=True)
	reference = models.CharField(max_length=100, null=True, blank=True, unique=True)
	remarques = models.TextField(null=True, blank=True)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	class Meta:
		db_table = 'reglement_enseignants'
		indexes = [models.Index(fields=['enseignant'], name='idx_reglement_enseignant')]

	def __str__(self):
		ref = self.reference or f"#{self.id}"
		return f"{ref} {self.montant} - {self.enseignant}"


# Ensure a stable, human-friendly reference is set after a reglement is created
@receiver(post_save, sender=ReglementEnseignants)
def reglement_post_save(sender, instance, created, **kwargs):
	# If reference missing, set it to REN{pk}
	if not instance.reference:
		try:
			sender.objects.filter(pk=instance.pk).update(reference=f"REN{instance.pk}")
		except Exception:
			pass


class Invoice(models.Model):
	inscription = models.ForeignKey(Inscription, on_delete=models.SET_NULL, db_column='inscription_id', null=True, blank=True, related_name='invoices')
	paiement = models.ForeignKey(Paiement, on_delete=models.SET_NULL, db_column='paiement_id', null=True, blank=True, related_name='invoices')
	numero = models.CharField(max_length=100, null=True, blank=True)
	montant = models.DecimalField(max_digits=10, decimal_places=2)
	date_emission = models.DateTimeField(null=True, blank=True)
	date_echeance = models.DateField(null=True, blank=True)
	statut = models.CharField(max_length=50, default='emi')
	remarques = models.TextField(null=True, blank=True)

	class Meta:
		db_table = 'invoice'

	def __str__(self):
		return f"INV {self.numero} - {self.montant}"


class PaymentPlan(models.Model):
	inscription = models.ForeignKey(Inscription, on_delete=models.CASCADE, db_column='inscription_id', related_name='payment_plans')
	total = models.DecimalField(max_digits=10, decimal_places=2)
	installments = models.IntegerField(default=1)
	created_at = models.DateTimeField(null=True, blank=True)
	note = models.TextField(null=True, blank=True)

	class Meta:
		db_table = 'payment_plan'

	def __str__(self):
		return f"Plan {self.id} - {self.total}"


class ProgressionModule(models.Model):
	inscription = models.ForeignKey(Inscription, on_delete=models.CASCADE, db_column='inscription_id', related_name='progressions')
	module = models.ForeignKey(Module, on_delete=models.CASCADE, db_column='module_id', related_name='progressions')
	complet = models.BooleanField(default=False)
	score = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
	date_completion = models.DateTimeField(null=True, blank=True)
	remarques = models.TextField(null=True, blank=True)

	class Meta:
		db_table = 'progression_module'
		unique_together = (('inscription', 'module'),)

	def __str__(self):
		return f"{self.inscription} - {self.module} ({'done' if self.complet else 'pending'})"


class Session(models.Model):
	formation = models.ForeignKey(Formation, on_delete=models.CASCADE, db_column='formation_id', related_name='sessions')
	salle = models.ForeignKey(Salle, on_delete=models.SET_NULL, db_column='salle_id', null=True, blank=True, related_name='sessions')
	formateur = models.ForeignKey(Utilisateur, on_delete=models.SET_NULL, db_column='formateur_id', null=True, blank=True, related_name='sessions')
	date_debut = models.DateField()
	date_fin = models.DateField(null=True, blank=True)
	horaire_debut = models.TimeField(null=True, blank=True)
	horaire_fin = models.TimeField(null=True, blank=True)
	statut = models.CharField(max_length=20, null=True, blank=True)

	class Meta:
		db_table = 'session'

	def __str__(self):
		return f"Session {self.id} - {self.formation}"

from django.db import models



class CalendarEvent(models.Model):
    id = models.AutoField(primary_key=True)
    titre = models.CharField(max_length=255)
    description = models.TextField(null=True, blank=True)
    start_datetime = models.DateTimeField()
    end_datetime = models.DateTimeField(null=True, blank=True)
    is_online = models.BooleanField(default=False)
    salle_id = models.PositiveIntegerField(null=True, blank=True)
    formation_id = models.PositiveIntegerField(null=True, blank=True)
    formation_name = models.CharField(max_length=255, null=True, blank=True)
    session_id = models.PositiveIntegerField(null=True, blank=True)
    organisateur_id = models.PositiveIntegerField(null=True, blank=True)
    formateur_name = models.CharField(max_length=255, null=True, blank=True)
    notifications_sent = models.BooleanField(default=False)
    groupe = models.CharField(max_length=100, null=True, blank=True)  # Newly added field
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)

    class Meta:
        db_table = 'calendar_event'
        indexes = [
            models.Index(fields=['start_datetime'], name='idx_event_start'),
            models.Index(fields=['salle_id'], name='idx_event_salle'),
            models.Index(fields=['formation_id'], name='idx_event_formation'),
            models.Index(fields=['session_id'], name='fk_event_session'),
            models.Index(fields=['organisateur_id'], name='fk_event_organisateur'),
        ]

    def __str__(self):
        return self.titre



class Support(models.Model):
	formation = models.ForeignKey(Formation, on_delete=models.SET_NULL, db_column='formation_id', null=True, blank=True, related_name='supports')
	module = models.ForeignKey(Module, on_delete=models.SET_NULL, db_column='module_id', null=True, blank=True, related_name='supports')
	titre = models.CharField(max_length=255)
	type = models.CharField(max_length=50, default='document')
	file_path = models.CharField(max_length=512, null=True, blank=True)
	url = models.CharField(max_length=1024, null=True, blank=True)
	meta = models.JSONField(null=True, blank=True)
	uploaded_by = models.ForeignKey(Utilisateur, on_delete=models.SET_NULL, db_column='uploaded_by', null=True, blank=True, related_name='uploads')
	created_at = models.DateTimeField(null=True, blank=True)

	class Meta:
		db_table = 'support'

	def __str__(self):
		return self.titre


class AvailabilityFormateur(models.Model):
	utilisateur = models.ForeignKey(Utilisateur, on_delete=models.CASCADE, db_column='utilisateur_id', related_name='availabilities')
	date = models.DateField(null=True, blank=True)
	day_of_week = models.SmallIntegerField(null=True, blank=True)
	start_time = models.TimeField(null=True, blank=True)
	end_time = models.TimeField(null=True, blank=True)
	note = models.CharField(max_length=255, null=True, blank=True)

	class Meta:
		db_table = 'availability_formateur'

	def __str__(self):
		return f"Availability {self.utilisateur} - {self.date or self.day_of_week}"


class Certification(models.Model):
	inscription = models.ForeignKey(Inscription, on_delete=models.CASCADE, db_column='inscription_id', related_name='certifications')
	numero_certificat = models.CharField(max_length=100, null=True, blank=True)
	fichier_certificat = models.CharField(max_length=512, null=True, blank=True)
	date_delivrance = models.DateTimeField(null=True, blank=True)
	valide_jusqua = models.DateField(null=True, blank=True)
	delivre_par = models.ForeignKey(Utilisateur, on_delete=models.SET_NULL, db_column='delivre_par', null=True, blank=True, related_name='certifs_delivres')

	class Meta:
		db_table = 'certification'

	def __str__(self):
		return self.numero_certificat or f"Certif {self.id}"



class Fournisseur(models.Model):
	nom = models.CharField(max_length=255)
	contact = models.CharField(max_length=255, null=True, blank=True)
	email = models.EmailField(max_length=255, null=True, blank=True)
	telephone = models.CharField(max_length=50, null=True, blank=True)
	adresse = models.TextField(null=True, blank=True)
	remarques = models.TextField(null=True, blank=True)
	created_at = models.DateTimeField(auto_now_add=True)

	class Meta:
		db_table = 'fournisseur'

	def __str__(self):
		return self.nom


class Achat(models.Model):
	fournisseur = models.ForeignKey(Fournisseur, on_delete=models.CASCADE, db_column='fournisseur_id', related_name='achats')
	date_achat = models.DateField(default=timezone.now)
	reference = models.CharField(max_length=100, null=True, blank=True, unique=True)
	total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
	statut = models.CharField(max_length=50, default='en_attente')
	remarques = models.TextField(null=True, blank=True)
	created_at = models.DateTimeField(auto_now_add=True)

	class Meta:
		db_table = 'achat'

	def __str__(self):
		return f"{self.reference or self.id} - {self.fournisseur}"


class AchatItem(models.Model):
	achat = models.ForeignKey(Achat, on_delete=models.CASCADE, db_column='achat_id', related_name='items')
	description = models.CharField(max_length=512)
	quantite = models.DecimalField(max_digits=10, decimal_places=2, default=1)
	prix_unitaire = models.DecimalField(max_digits=12, decimal_places=2, default=0)

	class Meta:
		db_table = 'achat_item'

	def montant(self):
		return (self.quantite or 0) * (self.prix_unitaire or 0)

	def __str__(self):
		return f"{self.description} x{self.quantite} @ {self.prix_unitaire}"


class ReglementFournisseur(models.Model):
	fournisseur = models.ForeignKey(Fournisseur, on_delete=models.CASCADE, db_column='fournisseur_id', related_name='reglements')
	achat = models.ForeignKey(Achat, on_delete=models.SET_NULL, db_column='achat_id', null=True, blank=True, related_name='reglements')
	montant = models.DecimalField(max_digits=12, decimal_places=2)
	date_reglement = models.DateField(default=timezone.now)
	mode = models.CharField(max_length=50, null=True, blank=True)
	reference = models.CharField(max_length=100, null=True, blank=True, unique=True)
	balance_after = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
	remarques = models.TextField(null=True, blank=True)

	class Meta:
		db_table = 'reglement_fournisseur'

	def __str__(self):
		return f"RF {self.reference or self.id} - {self.montant} - {self.fournisseur}"


# Post-save helpers: set stable reference and compute balance snapshot
@receiver(post_save, sender=Achat)
def achat_post_save(sender, instance, created, **kwargs):
	if not instance.reference:
		try:
			sender.objects.filter(pk=instance.pk).update(reference=f"ACH{instance.pk}")
		except Exception:
			pass


@receiver(post_save, sender=ReglementFournisseur)
def reglement_fournisseur_post_save(sender, instance, created, **kwargs):
	# set reference if missing
	if not instance.reference:
		try:
			sender.objects.filter(pk=instance.pk).update(reference=f"RFO{instance.pk}")
		except Exception:
			pass
	# compute balance_after as (sum of supplier achats totals) - (sum of supplier reglements)
	try:
		sup = instance.fournisseur
		total_achats = Achat.objects.filter(fournisseur=sup).aggregate(total=Sum('total'))['total'] or 0
		total_regles = ReglementFournisseur.objects.filter(fournisseur=sup).aggregate(total=Sum('montant'))['total'] or 0
		balance = float(total_achats) - float(total_regles)
		sender.objects.filter(pk=instance.pk).update(balance_after=balance)
	except Exception:
		pass






class Charge(models.Model):
	# corresponds to existing SQL table `charges`
	id = models.AutoField(primary_key=True, db_column='id_charge')
	type_charge = models.CharField(max_length=100)
	montant = models.DecimalField(max_digits=10, decimal_places=2)
	date_paiement = models.DateField()
	mode_paiement = models.CharField(max_length=50, null=True, blank=True)
	reference = models.CharField(max_length=100, null=True, blank=True)
	remarque = models.TextField(null=True, blank=True)
	# optional relation to Formation (nullable). Column added manually to your DB as `formation_id`
	formation = models.ForeignKey('Formation', null=True, blank=True, on_delete=models.SET_NULL, db_column='formation_id', related_name='charges')
	# contact phone or identifier (added to DB via ALTER TABLE as requested)
	contact = models.CharField(max_length=20, null=True, blank=True)
	# fournisseur (supplier name)
	fournisseur = models.CharField(max_length=255, null=True, blank=True)
	# nom contact (contact name) - stored in document/remarque as JSON
	nom_contact = models.CharField(max_length=255, null=True, blank=True)
	# attachment for documents or photos
	attachment = models.FileField(upload_to='charges_attachments/', null=True, blank=True)

	class Meta:
		db_table = 'charges'

	def __str__(self):
		return f"{self.type_charge} — {self.montant} € on {self.date_paiement}"
	


@receiver(post_save, sender=Charge)
def charge_post_save(sender, instance, created, **kwargs):
	# set automatic reference 'C<id>' if missing
	try:
		if not instance.reference:
			sender.objects.filter(pk=instance.pk).update(reference=f"C{instance.pk}")
	except Exception:
		pass


class Presence(models.Model):
	STATUT_CHOICES = (
		('present', 'Présent'),
		('absent', 'Absent'),
		('retard', 'Retard'),
	)

	etudiant = models.ForeignKey('Etudiant', on_delete=models.CASCADE, related_name='presences')

	calendar_event = models.ForeignKey('CalendarEvent', on_delete=models.CASCADE, related_name='presences')
	statut = models.CharField(max_length=10, choices=STATUT_CHOICES, default='present')
	remarques = models.TextField(null=True, blank=True)

	class Meta:
		db_table = 'presence'
		unique_together = ('etudiant', 'calendar_event')

	def __str__(self):
		return f"{self.etudiant} - {self.calendar_event.titre} : {self.statut}"




class Groupe(models.Model):
    nom = models.CharField(max_length=100, unique=True)
    description = models.TextField(null=True, blank=True)
    formation = models.ForeignKey(
        'Formation',
        on_delete=models.SET_NULL,
        db_column='formation_id',
        null=True,
        blank=True,
        related_name='groupes'
    )
    capacite = models.IntegerField(null=True, blank=True)  # nouveau champ capacité
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'groupe'
        verbose_name = "Groupe"
        verbose_name_plural = "Groupes"
        unique_together = (('nom', 'formation'),)

    def __str__(self):
        return f"{self.nom} ({self.formation.nom if self.formation else 'Sans formation'})"

    
    class Meta:
        db_table = 'groupe'
        verbose_name = "Groupe"
        verbose_name_plural = "Groupes"
        unique_together = (('nom', 'formation'),)

    def __str__(self):
        return f"{self.nom} ({self.formation.nom if self.formation else 'Sans formation'})"
