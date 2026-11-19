# Fomo Recap (cloud)

Outil d'observation : lit les trades **publics** faits via l'app Fomo sur la blockchain Solana,
repère les traders réguliers et envoie un récap / des alertes sur Telegram.
**Il ne passe aucun ordre** et n'a accès à aucun compte.

- Tourne toutes les 30 min avec GitHub Actions (`.github/workflows/fomo.yml`).
- Sources gratuites : nœud public Solana, DexScreener, RugCheck, GeckoTerminal.
- La base de données est rangée sur la branche `data` (un seul fichier, remplacé à chaque passe).
- Les clés Telegram sont dans les *secrets* du dépôt, jamais dans les fichiers.

Ce n'est pas un conseil en investissement.
