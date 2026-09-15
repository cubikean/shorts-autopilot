# Shorts Autopilot

Pipeline automatique de shorts : repère les vidéos qui décollent sur une liste
de chaînes YouTube / Twitch, en extrait les meilleurs moments en 9:16 sous-titrés,
les range dans Notion pour validation, puis publie sur YouTube ceux que tu valides.

```
07h00  discover  → Notion « Vidéos à traiter »   (vidéos virales repérées)
01h00  process   → Notion « Shorts » À publier   (mp4 + titre, description, hashtags)
 toi             → Publication = Validé
/4 h   publish   → YouTube                        (lien écrit dans Notion, statut Publié)
```

> Ne liste que des chaînes que tu as le droit de clipper (programmes de clipping,
> tes propres chaînes, accord écrit). Sinon : Content ID et démonétisation
> « contenu réutilisé ».

## Installation (Windows)

Prérequis :
- Python 3.10+
- `ffmpeg` dans le PATH
- [Claude Code](https://claude.com/claude-code) installé et connecté (`claude` une fois) — c'est lui qui choisit les moments et écrit les textes, sur ton abonnement Claude. OpenAI ou Gemini restent possibles via `LLM_PROVIDER`.
- Optionnel : GPU NVIDIA pour une transcription beaucoup plus rapide (aucun torch requis)

```powershell
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements-local.txt
copy .env.example .env
```

## Configuration

### 1. Notion

1. Crée une intégration interne sur <https://www.notion.so/my-integrations> et mets son token dans `NOTION_TOKEN`.
2. Partage une page vide avec l'intégration, puis :
   ```powershell
   python feed.py setup-notion "<URL de la page>"
   ```
3. Copie les deux ids affichés dans `.env` (`NOTION_VIDEOS_DB`, `NOTION_SHORTS_DB`).

### 2. Sources

Copie `sources.example.json` en `sources.json` et liste les chaînes :

```json
{
  "youtube": [{"handle": "@Gotaga"}, {"channel_id": "UCxxxxxxxxxxxxxxxxxxxxxx"}],
  "twitch": [{"login": "zerator"}]
}
```

Twitch est optionnel : crée une app sur <https://dev.twitch.tv/console/apps> et
renseigne `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET`, sinon Twitch est ignoré.

### 3. YouTube (publication)

1. Dans [Google Cloud Console](https://console.cloud.google.com) : active **YouTube Data API v3**.
2. **Google Auth Platform → Branding** : nom de l'app, e-mail d'assistance, contact développeur. Pas de logo (sinon validation Google obligatoire).
3. **Audience** : clique sur **Publier l'application** (en mode Test, le token expire au bout de 7 jours).
4. **Clients** : crée un client OAuth de type **Application de bureau**, télécharge le JSON en `client_secret.json` à la racine.
5. Puis, une seule fois :
   ```powershell
   python feed.py setup-publish   # ajoute Validé / Erreur, colonnes YouTube et dates dans Notion
   python feed.py auth-youtube    # consentement dans le navigateur, choisis la chaîne
   ```
   Sur « Google n'a pas validé cette application » : **Paramètres avancés → Accéder à l'app**.

`client_secret.json`, `youtube_token.json` et `.env` sont ignorés par git.

## Utilisation

### En automatique

```powershell
powershell -ExecutionPolicy Bypass -File scripts\schedule_windows.ps1
```

| Tâche | Quand | Rôle |
|---|---|---|
| `ShortsFeed-Process` | 01h00 | génère les shorts des vidéos en file |
| `ShortsFeed-Discover` | 07h00 | cherche les nouvelles vidéos virales |
| `ShortsFeed-Publish` | 09h, 13h, 17h, 21h, 01h, 05h | publie les shorts validés (`PUBLISH_MAX_PER_RUN` par passage) |

Les horaires se changent en paramètres du script (`-DiscoverAt`, `-ProcessAt`,
`-PublishFrom`, `-PublishEveryHours`). Le PC doit être allumé avec ta session
ouverte (il est réveillé de la veille). Logs : `output\logs\`.
Supprimer les tâches : `Unregister-ScheduledTask ShortsFeed-*`.

### À la main

| Commande | Rôle |
|---|---|
| `python feed.py discover --dry-run` | affiche les candidats sans rien écrire |
| `python feed.py discover` | ajoute les nouvelles vidéos dans Notion |
| `python feed.py process --limit 3` | génère les shorts des meilleures vidéos en file |
| `python feed.py publish --dry-run` | liste ce qui serait publié |
| `python feed.py publish --limit 1` | publie les shorts validés |

Pour tester un upload sans le rendre public :
`$env:YOUTUBE_PRIVACY="private"; python feed.py publish`

### Une vidéo précise, hors pipeline

```powershell
python main.py "https://www.youtube.com/watch?v=VIDEO_ID" --mode local --num-clips 3
python main.py "D:\Videos\stream.mp4" --mode local --layout stack
```

Toujours passer `--mode local` : le mode par défaut (`api`, MuAPI) vient du
projet d'origine et n'est pas utilisé ici. Les clips arrivent dans
`output\<id>\short_01.mp4` avec un `short_01.json` (titre, description, hashtags).

| Option | Défaut | Rôle |
|---|---|---|
| `--num-clips` | `3` | nombre de shorts |
| `--layout` | `auto` | `auto` : webcam de stream détectée placée au-dessus du jeu ; `single` : cadrage sur le visage ; `stack` : force la webcam en haut |
| `--no-subtitles` | — | désactive les sous-titres incrustés |
| `--language` | auto | force la langue de Whisper (`fr`, `en`…) |
| `--format` | `720` | résolution téléchargée : `360` / `480` / `720` / `1080` |
| `--aspect-ratio` | `9:16` | ratio de sortie |
| `--output-json` | — | écrit le résultat complet (transcription + tous les candidats) |

## Workflow Notion

**Vidéos à traiter** — `Statut` : À traiter → En cours → Prêt (ou Erreur, avec la raison).
Passe une vidéo en *Rejeté* pour qu'elle ne soit jamais traitée.

**Shorts** — `Publication` :

| Statut | Signification |
|---|---|
| À publier | généré, en attente de ta relecture |
| **Validé** | à toi de le mettre : il partira au prochain `publish` |
| Publié | en ligne, lien dans la colonne **YouTube** |
| Erreur | échec, raison dans *Erreur publication* ; remets *Validé* pour réessayer |
| Rejeté | ignoré |

Tu peux modifier les textes dans Notion avant de valider : c'est ce qui est publié.
Une **Date de publication** dans le futur programme la sortie sur YouTube.

| YouTube | Colonnes Notion |
|---|---|
| Titre | *Titre* (sinon *Accroche*), 100 caractères max |
| Description | « *Accroche* » + *Description* (avec crédit source) + *Hashtags* |
| Tags | *Hashtags* sans `#` |

## Comment ça marche

**Découverte**
- **YouTube** : flux RSS public des chaînes (sans clé ni quota). Score = vues/heure de la vidéo ÷ vues/heure médianes de la chaîne. Mise en file à partir de `FEED_MIN_SCORE` (1.5). Les vidéos déjà en format Short, en live ou hors `FEED_MIN/MAX_DURATION_MINUTES` sont ignorées.
- **Twitch** : clips les plus vus des dernières 24 h, fusionnés en « moments ». Seule une fenêtre autour du moment est téléchargée (pas le VOD entier) et donne un short.

**Génération**
1. **Téléchargement** avec `yt-dlp` (mis en cache : `output\source_<id>.mp4`).
2. **Transcription** `faster-whisper` : langue détectée, puis `large-v3` pour l'anglais et `large-v3-turbo` pour le reste (`LOCAL_WHISPER_MODELS`). Mise en cache en `.srt`.
3. **Choix des moments** par le LLM : grille de viralité (accroche, pic d'émotion, prise de position, révélation, conflit, punchline, chute d'histoire, astuce), clips de 20 à 180 s, sans chevauchement. Titre, description et hashtags écrits sur un ton ado, direct. Réponses mises en cache (`output\llm_cache`) : relancer la même vidéo ne coûte rien.
4. **Cadrage vertical** : détection de visage YuNet, un sujet suivi par plan, caméra fixe quand il bouge peu, coupes franches entre les plans. Webcam de stream détectée → empilée au-dessus du contenu.
5. **Sous-titres** mot par mot, en majuscules, avec effet « pop », dans la langue parlée.

**Publication** : upload YouTube reprenable (retries automatiques). Quand le
quota est épuisé, le passage s'arrête et les shorts restants attendent le suivant.

Pour ajuster le ton ou les critères : `HIGHLIGHT_SYSTEM_PROMPT` et
`VIRALITY_CRITERIA` dans `shorts_generator/highlights.py`.

## Réglages (`.env`)

Tout est documenté dans `.env.example`. Les plus utiles :

| Variable | Défaut | Rôle |
|---|---|---|
| `LLM_PROVIDER` | `openai` | `claude`, `openai` ou `gemini` |
| `CLAUDE_MODEL` / `CLAUDE_EFFORT` / `CLAUDE_THINKING` | `sonnet` / `low` / `off` | coût vs qualité des appels Claude |
| `LOCAL_WHISPER_DEVICE` | `auto` | `auto` (GPU si dispo), `cpu`, `cuda` |
| `SUBTITLE_FONT` / `SUBTITLE_POSITION` | `Arial Black` / `0.70` | police et hauteur des sous-titres (0 = haut, 1 = bas) |
| `FEED_MIN_SCORE` | `1.5` | seuil de viralité YouTube (0 = toutes les nouvelles vidéos) |
| `FEED_MAX_PER_RUN` / `FEED_CLIPS_PER_VIDEO` | `3` / `3` | vidéos traitées par nuit, shorts par vidéo |
| `PUBLISH_MAX_PER_RUN` | `1` | shorts publiés par passage |
| `YOUTUBE_PRIVACY` | `public` | `public`, `unlisted` ou `private` |
| `YOUTUBE_CATEGORY_ID` | `24` | 24 Divertissement, 20 Jeux vidéo, 23 Humour |

## Structure

```
feed.py                      CLI du pipeline (setup, discover, process, publish)
main.py                      CLI pour une vidéo isolée
scripts/schedule_windows.ps1 tâches planifiées Windows
shorts_generator/
├── config.py                lecture du .env
├── pipeline.py              enchaîne téléchargement → transcription → moments → rendu
├── highlights.py            prompt et sélection des moments (LLM)
├── local/                   downloader (yt-dlp), transcriber (whisper), llm, clipper (ffmpeg),
│                            reframe (cadrage visage / webcam), subtitles
├── feed/                    découverte YouTube / Twitch, client Notion, runner discover/process
└── publish/                 base (Publisher), youtube, runner publish
```

Les fichiers à la racine de `shorts_generator/` (`muapi.py`, `downloader.py`,
`transcriber.py`, `clipper.py`) ne servent qu'au mode `api` hérité.

### Ajouter une plateforme

Chaque plateforme = une classe `Publisher` (`publish(post) -> url`) dans
`shorts_generator/publish/`, plus une entrée dans `PLATFORMS`
(`publish/runner.py`) avec le nom de sa colonne URL dans Notion. Ajoute-la à
`PUBLISH_PLATFORMS` et relance `python feed.py setup-publish`. TikTok est la prochaine.

## Dépannage

| Problème | Solution |
|---|---|
| `Erreur 403 : access_denied` à l'autorisation | app Google en mode Test : publie-la (voir Configuration → YouTube) |
| `YouTube token expired or revoked` | `python feed.py auth-youtube` |
| Vidéos toujours privées sur YouTube | projet Google non audité : uploads bloqués en privé. Demande l'[audit de l'API YouTube](https://support.google.com/youtube/contact/yt_api_form) ou passe-les en public dans Studio |
| `quotaExceeded` | quota YouTube (~6 uploads/jour) : ça repart le lendemain |
| `Fichier introuvable` dans Notion | le mp4 a été déplacé ou supprimé de `output\shorts\` |
| `LLM_PROVIDER=claude needs the Claude Code CLI` | installe Claude Code et lance `claude` une fois |
| Whisper ne produit aucun segment | pas de parole détectée : essaie `--language fr` |
| Tâche planifiée qui ne fait rien | regarde `output\logs\ShortsFeed-*.log` |

## Licence

MIT. Basé sur [SamurAIGPT/AI-Youtube-Shorts-Generator](https://github.com/SamurAIGPT/AI-Youtube-Shorts-Generator).
