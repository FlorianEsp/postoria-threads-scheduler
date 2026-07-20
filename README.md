# Postoria Threads Scheduler

Application locale Streamlit pour programmer automatiquement des posts Threads via l'API Postoria.

## Fonctionnalités

- Import CSV avec `text` recommandé, `caption` compatible, ou première colonne utilisée par défaut
- Variables CSV avec `{nom_colonne}` dans les textes
- Page Photos séparée avec groupes photo indépendants des groupes de comptes
- Quota photo global ou différent par compte, répété pour chaque jour planifié
- Rotation persistante: toutes les photos prêtes sont parcourues avant réutilisation
- Espacement minimum des photos exprimé en pourcentage des posts du compte
- Upload automatique vers Postoria et sauvegarde des fichiers dans Supabase Storage
- Retrait, remplacement et déplacement d'une photo directement dans la preview
- Thread chains en preview via colonnes `reply_1`, `reply_2`, etc.
- Bibliothèque locale de posts SQLite
- Sélection manuelle des posts; les photos sont attribuées depuis leur groupe dédié
- Récupération des comptes Threads depuis Postoria
- Sélection des comptes dans un tableau type Postoria
- Groupes libres directement dans le tableau comptes
- Questions de démarrage avant preview
- Planning automatique sur une journée
- Intervalle minimum entre deux posts du même compte
- Option anti-répétition du même texte dans une fenêtre réglable entre comptes
- Mode exact ou range pour le nombre de posts par compte
- Preview tableau + calendrier visuel
- Mode dry-run
- Programmation via API Postoria
- Vérification des statuts
- Suppression des posts programmés
- Désactivation d'un compte après 2 échecs consécutifs pendant l'envoi

## Installation

```bash
python -m venv .venv
source .venv/bin/activate  # macOS/Linux
# .venv\Scripts\activate  # Windows
pip install -r requirements.txt
cp .env.example .env
```

Modifie `.env` :

```bash
POSTORIA_API_KEY=pst_live_ta_nouvelle_cle
POSTORIA_BASE_URL=https://api.postoria.io/v1
APP_TIMEZONE=Europe/Brussels
```

Important : ne mets jamais `.env` sur GitHub.

### Groupes et photos persistants sur Streamlit Cloud (optionnel)

Sur Streamlit Community Cloud, le fichier SQLite local peut être recréé après un redéploiement ou un redémarrage. Pour conserver les groupes, leurs couleurs et l'appartenance de chaque compte, crée un projet Supabase puis exécute une fois cette requête dans le SQL Editor :

```sql
create table if not exists public.scheduler_group_configs (
  workspace_id text primary key,
  config jsonb not null default '{"groups": [], "accounts": []}'::jsonb,
  updated_at timestamptz not null default now()
);

alter table public.scheduler_group_configs enable row level security;
```

Ajoute ensuite ces secrets dans Streamlit Cloud, dans **App settings > Secrets** (jamais dans GitHub) :

```toml
SUPABASE_URL = "https://ton-projet.supabase.co"
SUPABASE_SERVICE_KEY = "ta_service_role_key"
```

Après avoir choisi le workspace, clique sur **Charger comptes Threads** : l'app récupère les comptes Postoria, puis restaure les groupes, leurs règles et la bibliothèque photo pour ce workspace. Le bucket privé `postoria-photo-library` est créé automatiquement avec la clé `service_role`. Sans ces secrets, l'application continue de fonctionner avec la base SQLite locale, mais les fichiers photo ne survivent pas forcément à un redéploiement Streamlit Cloud.

## Lancement

```bash
streamlit run app.py
```

Puis ouvre l'URL localhost affichée par Streamlit.

## Format CSV

```csv
text
"Premier post Threads"
"Deuxième post Threads"
```

Avec photos déjà présentes dans Postoria :

```csv
text,media_ids,media_folder,firstname,city,reply_1,reply_2
"Bonjour {firstname} de {city}","12345","","Lucas","Paris","Réponse 1","Réponse 2"
"Deuxième post Threads","","selfies_jade","Emma","Lyon","",""
```

Si `media_ids` est vide et `media_folder` rempli, l'app prend un media ID au hasard dans ce dossier.

## Règles de planning

- Threads uniquement
- Texte ou media IDs déjà disponibles côté Postoria
- Les réponses de thread chain sont visibles en preview. L'envoi API actuel publie le post principal tant que l'endpoint replies Postoria n'est pas confirmé.
- Un même texte ne peut pas être utilisé deux fois sur le même compte le même jour
- Optionnel : un même texte peut être bloqué sur plusieurs comptes pendant une fenêtre réglable
- Si le timeframe est trop court, l'app refuse
- Une photo en preview est réservée. Elle n'est comptée comme utilisée qu'après acceptation du post par Postoria.
- Une erreur d'envoi garde la photo réservée pour le retry; la retirer ou la remplacer la rend disponible.
- Les horaires Europe/Brussels sont convertis en UTC pour Postoria

## Note sécurité

Si une clé API a été partagée par erreur, révoque-la dans Postoria et crée une nouvelle clé.
