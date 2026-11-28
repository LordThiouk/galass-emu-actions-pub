# L6 — gaps OTP / Bridges (scénario lot Artemis)

Papa GO 10:20 : L6 optionnel seulement si déjà instrumenté. **État harnais : non instrumenté.**

| ISC / Bridge | Manque dans le harnais |
|---|---|
| ISC-50 OTP | Pas de flux OTP / SMS dans `lot_flow.sh` ni marqueur P0_LOT |
| ISC-80 | Pas de point e2e ni capture dédiée |
| ISC-81 | Idem |
| ISC-82 / 82.1 | Idem |
| ISC-83 | Idem |
| ISC-84 | Idem |
| Bridge-1 | Pas de marqueur / cap Bridge-1 |
| Bridge-2 | Pas de marqueur / cap Bridge-2 |
| Bridge-4 | Pas de marqueur / cap Bridge-4 |

Pour les ajouter : déclarer des points **hors** `P0_DEFAULT` (étendre `P0_LOT` ou `P0_LOT_L6`), taps UI + sidecars MATCH, sans toucher aux seuils P0 multistop.
