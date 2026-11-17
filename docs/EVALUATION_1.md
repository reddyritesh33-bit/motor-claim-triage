# Evaluation 1: corpus confirmation and the difficult case (section 4.1)

## Corpus (one insurer, one product)

| Document | Source (official insurer site) | Pages | sha256 (first 12) |
|---|---|---|---|
| Universal Sompo **Motor Private Car Package Policy Wording**, UIN IRDAN134RP0004V03200708 (includes *Section IX: Claim Procedure*, the insurer's claims procedure) | universalsompo.com/assets/file/motor-private-car/motor-private-car-policy-wordings.pdf | 60 | a4510a2dabfb |
| Universal Sompo **Motor Private Car Package Prospectus**, same UIN (states the permitted use of the vehicle) | universalsompo.com/assets/file/motor-private-car/motor-private-car-prospectus.pdf | 49 | e9134d913c6a |

Both PDFs have a text layer, so no OCR is needed. Manifest: `corpus/manifest.yaml`.

**Why this corpus.** We checked several insurers before choosing. In every package-policy
wording we looked at (New India, SBI General, Raheja QBE, Tata AIG, Liberty, Magma), the
cover and the general exclusion that refers to the "Limitations as to Use" appear, but the
limitation's *content* sits in the policy schedule or certificate, which is per-customer
and not published. Universal Sompo publishes it in the product prospectus. That puts the
deciding provision in a different document from the cover it limits, which is exactly the
retrieval difficulty the brief describes. (ICICI Lombard blocks automated downloads; see
the incident log.)

## The passages the case depends on

**Case text:** *"I was making a food delivery run in my own car when the accident happened."*

| # | Clause id | Where | Verbatim text | Role |
|---|---|---|---|---|
| 1 | `PW-S1-001` | Policy Wording, Section I (p.1) | "The Company will indemnify the insured against loss or damage to the vehicle insured hereunder and / or its accessories whilst hereon; ... vi. by accidental external means;" | the cover that *appears* to apply |
| 2 | `PW-S5-001` | Policy Wording, Section V General Exclusions, item 3(a) (p.40) | "The Company shall not be liable under this policy in respect of ... 3 any accidental loss damage and/or liability caused sustained or incurred whilst the vehicle insured herein is a. being used otherwise than in accordance with the 'Limitations as to Use'" | the **modifier**, in a different section |
| 3 | `PR-S0-001` | Prospectus, opening paragraph (p.1) | "Private Car Vehicle Insurance Policy covers Vehicles used for social, domestic and pleasure purpose and also for professional purpose (excluding carriage of goods other than samples) of the insured ... but excluding use for hire or reward, racing, pace making, reliability trial, speed testing and use for any purpose in connection with the Motor Trade." | the **content** of the Limitations as to Use, in a different document. It never uses the phrase "Limitations as to Use" |

Measured retrieval behaviour (hybrid index, top 5):

| Query | Returns #2? | Returns #3? |
|---|---|---|
| the case text itself (single pass) | no | no. Top hits are roadside-assistance and personal-accident add-ons |
| "Limitations as to Use" | yes (rank 2) | **no** |
| derived query: "Limitations as to Use: vehicle used for social domestic pleasure purpose excluding use for hire or reward carriage of goods" | yes (rank 2) | **yes (rank 1)** |

## Test values (scenario `S03` in `eval/scenarios.yaml`)

```json
{
  "claim_reference": "FNOL-<generated>",
  "channel": "app",
  "policy": {"policy_number": "USGI/PC/2026/004512", "product_uin": "IRDAN134RP0004V03200708",
             "policy_start_date": "2026-04-01", "policy_end_date": "2027-03-31",
             "vehicle_class": "private_car", "idv_inr": 650000, "addons": []},
  "vehicle": {"registration_number": "KA01AB1234", "make": "Maruti Suzuki", "model": "Swift VXI",
              "year_of_manufacture": 2022, "fuel_type": "petrol"},
  "incident": {"date_of_loss": "2026-10-20T19:30:00+05:30", "reported_at": "2026-10-21T10:00:00+05:30",
               "location": {"city": "Bengaluru", "state": "Karnataka", "pincode": "560034"},
               "description": "I was making a food delivery run in my own car when the accident happened. A bike cut in front of me at the Koramangala signal and I braked hard and hit the divider. Front bumper and left fender are damaged.",
               "police_report_filed": false, "fir_number": null, "third_party_involved": false,
               "injuries": "none", "vehicle_driveable": true, "estimated_repair_cost_inr": 42000,
               "cashless_requested": true},
  "driver": {"driver_is_insured": true, "name": "Arjun Rao", "licence_number": "KA0120150012345",
             "licence_valid_until": "2035-05-01"},
  "claimant": {"name": "Arjun Rao", "phone": "9876543210", "email": "arjun.rao@example.com"}
}
```

## Reference answer (to be confirmed at Evaluation 1)

| Field | Expected |
|---|---|
| claim_type | `own_damage` |
| coverage_position | `likely_not_covered`. Commercial food delivery is use for hire or reward and carriage of goods, outside the permitted use, so General Exclusion 3(a) applies |
| route | `investigation` (the facts of use must be established before a handler decides) |
| citations | must include **`PW-S5-001`** (Exclusion 3(a)) and **`PR-S0-001`** (permitted use), plus the cover clause |
| escalation | not escalated (verified). A human handler still decides; the output is advisory |

A system that answers from #1 alone (fast-track, covered) fails this case. The verifier
enforces it: check V6 rejects any draft that cites Exclusion 3(a) without having searched
for, and cited, the passage that gives "Limitations as to Use" its content. Check V7
rejects a draft that ignores the commercial-use fact.
