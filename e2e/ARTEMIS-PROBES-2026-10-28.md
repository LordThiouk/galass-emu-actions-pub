# Mesures directes FreeLLMAPI (2026-10-28, heure Dakar/UTC)

Sondes HTTP directes sur le relais :3001 depuis la box, clé via env (jamais loggée), timeout 60 s, max_tokens 10.

| modèle | texte | image | statut chaîne |
|---|---|---|---|
| gpt-4.1 | 3,0 s 200 | 2,2 s 200 | tête |
| gemini-2.5-flash | 2,7 s 200 | 2,2 s 200 | 2e |
| gpt-4o | 3,8 s 200 | 429 (rate-limit) | 3e |
| gemini-2.5-flash-lite | 404 disabled | — | DEAD |
| gemini-2.0-flash-lite | 404 hors catalogue | — | DEAD |
| gemini-3.5-flash-lite (19:0x) | 502 | — | DEAD (retiré) |
| gemini-3.1-flash-lite (19:0x) | 429 | — | DEAD (retiré) |
| gemini-flash-lite (19:0x) | 429 | — | DEAD (retiré) |

Conclusion : la tête de chaîne répond en < 4 s, très sous la borne par requête (13 s).
Les timeouts Artemis ~33 s des runs 40602473815 / 40669473919 ne viennent pas de la
latence modèle ; le plancher de latence est une protection de bord, pas la rémission.
Chaîne par défaut réduite à `gpt-4.1,gemini-2.5-flash,gpt-4o`.
