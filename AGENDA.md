# Agenda

Suivis datés et idées en attente pour le pipeline. À relire en début de session :
traiter ce qui est échu, cocher, dater ce qu'on ajoute.

## Rendez-vous

- [ ] **2026-10-14 — Twitch.** Vérifier qu'aucune vidéo Twitch n'est repassée en *Erreur* avec
  « section download produced no usable video » depuis le correctif `a6db595` (coupe sur playlist
  réduite, contourne le rebouclage des timestamps après 26,5 h de VOD).
- [ ] **2026-10-17 — base de comparaison complète.** Les 13 shorts du 07/10 (ancien prompt, avant
  `9e02d4f`) auront 10 jours. Lancer `python feed.py replay` (ou attendre la tâche Stats de 08h) et
  noter la médiane de la semaine du 2026-10-05. Base actuelle : **centile médian 58/100 sur 7 shorts
  (semaine du 2026-09-21), 0/7 dans le top 10 %** — à peine mieux que le hasard (50).
- [ ] **2026-10-19 — premiers shorts du nouveau prompt notés.** Ceux rendus à partir du 08/10
  (commentaires horodatés + repères audio + critères divertissement + reclassement final).
  Vérifier que *Centile revu* / *Pic revu* sont bien remplis et lire les cas sous le centile 30.
- [ ] **2026-10-29 — verdict sur le nouveau prompt.** La semaine du 2026-10-12 est la première
  100 % nouveau prompt. Comparer sa médiane à la base. Objectif : médiane ≥ 70 et ≥ 1 short sur 4
  dans le top 10 %. Si ça ne bouge pas, revoir le poids donné aux zones chaudes dans le prompt.

## Idées en attente

- **Apprendre de nos stats** : injecter dans le prompt nos 3 meilleurs et 3 pires shorts (colonne
  *Stats*) et caler la durée cible (45-90 s aujourd'hui) sur ce qui marche vraiment chez nous.
- **Chapitres YouTube** comme découpage naturel quand la vidéo en a (Aywen en met une douzaine).
- **Densité du chat Twitch** pour les VOD, en plus des clips des viewers.
- **Gags visuels invisibles** : le texte rate les gags de montage (ex. Aywen 8:50, 126 likes, classé
  4e). Piste : quand une zone chaude très likée n'a rien de marquant dans la transcription, la
  proposer quand même au reclassement.
- **Hallucinations Whisper en outro** (« Sous-titrage ST' 501 ») marquées *VERY LOUD* : les filtrer
  avant l'analyse audio si ça finit par fausser un choix.
- **Python 3.10 obsolète pour yt-dlp** (avertissement à chaque appel) : passer le venv en 3.11+
  avant qu'une version de yt-dlp ne l'abandonne.
