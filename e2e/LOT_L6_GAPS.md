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

## ISC-85 (Rider caméra profil) — hors L6

ISC-85 (ISA parent `2026-11-22-galass-lot-client`) = photo profil Rider via caméra intégrée.
Couverture harnais prévue = **L1** `lot-rider-camera-ok` (pas un SKIP L6).
Manque aujourd’hui côté app : preuve `flutter test` ImageSource.camera + capture appareil ; L1 a échoué sur essai 3 à cause ANR Pixel Launcher (infra), pas d’un SKIP explicite ISC-85.

Pour étendre L6 : déclarer des points **hors** `P0_DEFAULT` (étendre `P0_LOT` ou `P0_LOT_L6`), taps UI + sidecars MATCH, sans toucher aux seuils P0 multistop.
