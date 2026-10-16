"""Tests for the 16 categories/topics researched in category_vocabulary.json:
the 12 Policy Master categories that previously had zero bill-evidence
vocabulary at all (document_required=True, so Category Consistency Check
could never PASS or EXCEPTION for them before this work - only ever
REVIEW_REQUIRED), plus targeted broadening of Fuel, Lodging, and the
TRANSPORT CHARGES mapping correction (see category_map.py's comment on why
it moved off the "Bus" label).

Each category gets: a positive match using common terminology, a positive
match using alternate/industry terminology, and at least one negative/
exclusion case drawn directly from category_vocabulary.json's
exclusion_or_conflicting_evidence - the specific false positives the
research explicitly warned against (bare VISA/MS/EMD/SUBSCRIPTION/
REGISTRATION/PETROLEUM/etc.). These test the actual classification
functions (_find_category, expected_bill_label), not just a vocabulary-file
lookup.
"""
from pathlib import Path

import pytest

from app.services import category_map
from app.services.extraction import bill_parser

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ---------- expected_bill_label mapping: all 12 newly-mapped categories ----------


@pytest.mark.parametrize(
    "category,label",
    [
        ("TOLL TAX CHARGES", "Toll"),
        ("CALIBRATION CHARGE", "Calibration"),
        ("INTERNET CHARGES", "Internet"),
        ("TELEPHONE ACCESSORIES", "Phone Accessories"),
        ("REPAIRS - COMPUTERS", "Computer Repair"),
        ("RATES & TAXES", "Statutory Tax"),
        ("MEMBERSHIP & SUBSCRIPTION FEE", "Membership"),
        ("TENDER FEE", "Tender Fee"),
        ("TRAVEL-OTHERS", "Travel Miscellaneous"),
        ("MARKETING - SEMINAR EXPS", "Seminar/Conference"),
        ("MARKETING EXPENSES - OTHERS", "Marketing Collateral"),
        ("SERVICE PURCHASES/SERVICE CHARGE", "Service Purchase"),
    ],
)
def test_newly_mapped_categories_have_expected_bill_label(category, label):
    """Before this work, every one of these returned None - Category
    Consistency Check could structurally never PASS or EXCEPTION for them."""
    assert category_map.expected_bill_label(category) == label


def test_transport_charges_no_longer_maps_to_bus():
    """TRANSPORT CHARGES previously collapsed onto the same bare "Bus" label
    TRAIN / BUS FARE owns for passenger bus tickets - real-world goods/
    freight evidence (consignment note, GTA, lorry hire) is what this
    category actually needs."""
    assert category_map.expected_bill_label("TRANSPORT CHARGES") == "Freight/Transport"
    assert category_map.expected_bill_label("TRANSPORT CHARGES") != "Bus"


# ---------- TOLL TAX CHARGES ----------


def test_toll_positive_common_terminology():
    assert bill_parser._find_category("NHAI Toll Plaza\nVehicle Class: Car\nAmount: Rs.65") == ("Toll", "NHAI")


def test_toll_positive_fastag_terminology():
    label, _ = bill_parser._find_category("NETC FASTag Deduction\nPlaza: Kherki Daula\nAmount: 95")
    assert label == "Toll"


def test_toll_excludes_road_tax():
    """Road Tax is an RTO vehicle-registration tax, not a toll - the two must
    never be conflated despite sharing the word 'Tax' (category_vocabulary.json
    TOLL TAX CHARGES exclusion note)."""
    label, _ = bill_parser._find_category("RTO Office\nRoad Tax Renewal Receipt\nVehicle Registration")
    assert label != "Toll"


# ---------- TRANSPORT CHARGES (Freight/Transport) ----------


def test_freight_positive_common_terminology():
    label, _ = bill_parser._find_category("Goods Transport Agency\nConsignment Note No: GTA-4521\nFreight: 5000")
    assert label == "Freight/Transport"


def test_freight_positive_alternate_terminology():
    label, _ = bill_parser._find_category("ABC Transport Contractor\nLorry Receipt\nLR No: 88213\nCartage: 2500")
    assert label == "Freight/Transport"


def test_freight_excludes_passenger_bus_ticket():
    """A passenger bus ticket (seat/PNR/stations) belongs to TRAIN / BUS FARE,
    never TRANSPORT CHARGES - the old bare-BUS mapping was exactly this
    over-broad collision."""
    label, _ = bill_parser._find_category("State Transport Corporation\nBus Ticket\nSeat No: 24\nPNR: 998211")
    assert label != "Freight/Transport"


def test_freight_excludes_passenger_cab_hire():
    """A point-to-point passenger cab/taxi invoice is CAB CHARGES, not
    TRANSPORT CHARGES, even when it says 'vehicle hire'."""
    label, _ = bill_parser._find_category("Ola Outstation\nVehicle Hire for personal travel\nRide Fare: 1200")
    assert label == "Cab/Taxi"


# ---------- Fuel broadening (brand names, guarded PETROLEUM, OCR fuzzy fallback) ----------


def test_fuel_brand_name_indian_oil():
    label, _ = bill_parser._find_category("IndianOil\nHigh Speed Diesel\nRate(Rs/L): 94.50")
    assert label == "Fuel"


def test_fuel_brand_name_bharat_petroleum_speed():
    label, _ = bill_parser._find_category("Bharat Petroleum\nSpeed 97\nVolume(L): 20.5")
    assert label == "Fuel"


def test_fuel_petroleum_guarded_by_dispenser_context():
    """A dealer name ending in 'Petroleum' only counts as fuel evidence when a
    fuel-dispenser field (nozzle/rate/volume/density/product) is also on the
    page - this is the real confirmed case (ER-40782), reproduced here
    without relying on the brand-name match so the guard itself is proven."""
    text = "UNNATI PETROLEUM\nRajasthan Hospital Road\nProduct: Diesel\nNozzle No.: 01\nRate(Rs/L): 97.92"
    label, _ = bill_parser._find_category(text)
    assert label == "Fuel"


def test_fuel_petroleum_unguarded_does_not_match():
    """A company whose trade name happens to end in 'Petroleum' but has no
    fuel-dispenser context on the page must NOT be treated as a fuel
    purchase (category_vocabulary.json's explicit false-positive warning)."""
    text = "Unnati Petroleum Logistics Pvt Ltd\nConsultancy Invoice\nProfessional Fees: 15000"
    label, _ = bill_parser._find_category(text)
    assert label != "Fuel"


def test_fuel_ms_is_never_a_standalone_keyword():
    """'MS' (Motor Spirit) collides with 'M/S' (Messrs), an extremely common
    Indian invoice company-name prefix - must never be added as a bare
    keyword (category_vocabulary.json's explicit warning)."""
    label, _ = bill_parser._find_category("M/S Sharma Enterprises\nTax Invoice\nTotal: Rs. 5000")
    assert label != "Fuel"


def test_fuel_ocr_fused_leading_character_recovered_via_fuzzy_fallback():
    """Real confirmed OCR defect (ER-40782 page 4): a stray character fused
    directly onto "Nozzle" with no space defeats the strict \\b-anchored
    regex. Reproduced here with no brand name present, so only the fuzzy
    fallback (not the brand-name match) can recover it."""
    label, snippet = bill_parser._find_category("Receipt No: 098\n\xc5Nozzle\nNo. :01\nAmount(Rs): 03900.15")
    assert label == "Fuel"
    assert snippet.upper() == "NOZZLE"


def test_fuel_ocr_inserted_space_recovered_via_fuzzy_fallback():
    """Real confirmed OCR defect: "Diesel" OCR'd as "Diese l" (inserted
    space) splits it into two tokens, neither of which equals "DIESEL" -
    edit-distance-1 fuzzy matching on the isolated token recovers it."""
    label, _ = bill_parser._find_category("Product\nDensity\n: Diese l\n829.9Kg/Cu.mtr")
    assert label == "Fuel"


def test_fuzzy_fallback_never_fires_when_strict_pass_already_matched():
    """The fuzzy fallback must only run when the strict pass found nothing -
    an exact, unambiguous "PETROL" must never be second-guessed."""
    label, snippet = bill_parser._find_category("Petrol Pump Receipt\nProduct: Petrol\nAmount: 500")
    assert label == "Fuel"
    assert snippet.upper() == "PETROL"


def test_real_pdf_er40782_indianoil_page_now_passes_category_consistency(client):
    """Real-world regression: this exact page (ER-40782, OWN CAR PETROL claim)
    previously resolved category_guess=None ("insufficient to verify") purely
    because of the OCR-fused-character defect - confirmed via a live audit of
    this repo's real corpus before this change. Must now extract Fuel."""
    pdf_path = REPO_ROOT / "ER-40782.pdf"
    if not pdf_path.exists():
        pytest.skip("real corpus PDF not present in this checkout")
    resp = client.post(
        "/api/uploads/expense-reports",
        files=[("files", (pdf_path.name, pdf_path.read_bytes(), "application/pdf"))],
    )
    report_id = resp.json()["results"][0]["document_id"].split(":")[1]
    bills = client.get(f"/api/expense-reports/{report_id}/bills").json()
    page4 = next((b for b in bills if b["page_number"] == 4), None)
    assert page4 is not None, "page 4 of ER-40782.pdf should produce a bill record"
    assert page4["category_guess"] == "Fuel"


# ---------- Lodging broadening ----------


def test_lodging_folio_is_positive_evidence():
    label, _ = bill_parser._find_category("Guest Folio\nFolio No: 4821\nRoom 204")
    assert label == "Lodging"


def test_lodging_brand_suffix_words_deliberately_not_added():
    """Research explicitly recommended AGAINST bare Inn/Suites/Resort/
    Residency/Grand/Regency/Palace as standalone keywords - they are
    proprietary brand-tier names with real collision risk (e.g. 'Grand'
    collides with 'Grand Total' on virtually every invoice). This is a
    deliberate regression guard: these words must stay absent from the
    Lodging pattern."""
    label, _ = bill_parser._find_category("Grand Total: Rs. 2000\nThank you for shopping")
    assert label != "Lodging", "'Grand' (from 'Grand Total') must never be read as lodging evidence"

    # The real confirmed residual gap: a UPI transfer to a business whose
    # name alone contains a brand-suffix word ("Palm Grove Suites Inn") with
    # no other lodging-structural text at all must stay unresolved rather
    # than being force-matched on the suffix words alone - per
    # category_vocabulary.json, this is a known limitation the keyword
    # vocabulary deliberately does not try to close.
    label, _ = bill_parser._find_category("To Palm Grove Suites Inn\nUPI ID: eazypay.584913851@icici\nAmount: 8820")
    assert label != "Lodging"


# ---------- INTERNET CHARGES ----------


def test_internet_positive_common_terminology():
    label, _ = bill_parser._find_category("Jio Fiber\nBroadband Plan Rental\nAmount: 999")
    assert label == "Internet"


def test_internet_positive_alternate_terminology():
    label, _ = bill_parser._find_category("BSNL\nLeased Line (1:1)\nBandwidth: 160 Mbps\nInvoice")
    assert label == "Internet"


def test_internet_excludes_bare_mobile_recharge():
    """A mobile/cellular recharge or postpaid bill (voice+SMS, no broadband/
    fiber/router component) is a telephone service expense, not INTERNET
    CHARGES, even under the same brand (Jio/Airtel bundle both)."""
    label, _ = bill_parser._find_category("Airtel Prepaid Recharge\nMobile Number: 9876543210\nValidity: 28 days")
    assert label != "Internet"


# ---------- TELEPHONE ACCESSORIES ----------


def test_phone_accessories_positive_common_terminology():
    label, _ = bill_parser._find_category("Amazon.in Tax Invoice\nMobile Charger Type-C\nQty: 1\nHSN 8504")
    assert label == "Phone Accessories"


def test_phone_accessories_positive_alternate_terminology():
    label, _ = bill_parser._find_category("Croma Retail\nTempered Glass Screen Guard\nPower Bank 10000mAh")
    assert label == "Phone Accessories"


def test_phone_accessories_excludes_mobile_recharge():
    label, _ = bill_parser._find_category("Jio Prepaid Recharge\nPlan: 239\nValidity: 24 days")
    assert label != "Phone Accessories"


def test_phone_accessories_excludes_internet_router():
    """A router/modem billed by an ISP as part of a connection is INTERNET
    CHARGES evidence, not a standalone accessory purchase."""
    label, _ = bill_parser._find_category("ACT Fibernet\nBroadband Connection\nRouter included in plan")
    assert label != "Phone Accessories"


# ---------- REPAIRS - COMPUTERS ----------


def test_computer_repair_positive_common_terminology():
    label, _ = bill_parser._find_category("Laptop Repair Invoice\nMotherboard Repair\nLabour: 1200")
    assert label == "Computer Repair"


def test_computer_repair_positive_alternate_terminology():
    label, _ = bill_parser._find_category("ASUS Authorized Service Center\nDiagnostic Fee: 250\nScreen Replacement")
    assert label == "Computer Repair"


# ---------- RATES & TAXES ----------


def test_rates_taxes_positive_common_terminology():
    label, _ = bill_parser._find_category("Municipal Corporation\nProperty Tax Receipt\nAssessment Year 2026-27")
    assert label == "Statutory Tax"


def test_rates_taxes_positive_alternate_terminology():
    label, _ = bill_parser._find_category("Professional Tax Challan\nState: Maharashtra\nPeriod: Q2 2026")
    assert label == "Statutory Tax"


def test_rates_taxes_excludes_routine_invoice_gst_line():
    """A CGST/SGST line on an ordinary purchase invoice is routine
    transactional GST, not 'Rates & Taxes' as a statutory expense head
    (category_vocabulary.json's explicit exclusion note)."""
    label, _ = bill_parser._find_category("Retail Invoice\nCGST 9% SGST 9%\nTotal: Rs. 500")
    assert label != "Statutory Tax"


# ---------- CALIBRATION CHARGE ----------


def test_calibration_positive_common_terminology():
    label, _ = bill_parser._find_category("Calibration Certificate No: CC-2024-108\nInstrument: Pressure Gauge")
    assert label == "Calibration"


def test_calibration_positive_alternate_terminology():
    label, _ = bill_parser._find_category("NABL Accredited Laboratory\nISO/IEC 17025:2017\nTraceability Certificate")
    assert label == "Calibration"


def test_calibration_excludes_generic_amc():
    """'AMC'/maintenance wording alone with no calibration/certificate
    language is more likely REPAIRS - COMPUTERS or SERVICE PURCHASES."""
    label, _ = bill_parser._find_category("Annual Maintenance Contract\nComputer Servicing")
    assert label != "Calibration"


# ---------- MEMBERSHIP & SUBSCRIPTION FEE ----------


def test_membership_positive_common_terminology():
    label, _ = bill_parser._find_category("ICAI Annual Fee Circular\nMembership Fee Receipt\nFY 2026-27")
    assert label == "Membership"


def test_membership_positive_alternate_terminology():
    label, _ = bill_parser._find_category("Certificate of Practice Fee\nCOP Fee Receipt\nICSI Member Portal")
    assert label == "Membership"


def test_membership_excludes_saas_subscription():
    """Software/SaaS subscription invoices ('Enterprise Plan', per-user
    billing) must not be read as professional membership - both use the word
    'subscription' but are structurally different (category_vocabulary.json
    exclusion note)."""
    label, _ = bill_parser._find_category("Enterprise Plan Subscription\nPer User Per Month Billing\nInvoice")
    assert label != "Membership"


# ---------- TENDER FEE ----------


def test_tender_fee_positive_common_terminology():
    label, _ = bill_parser._find_category("Notice Inviting Tender\nTender Fee (DD/BC in original): Rs 1180")
    assert label == "Tender Fee"


def test_tender_fee_positive_alternate_terminology():
    label, _ = bill_parser._find_category("Cost of Tender Document: Rs. 2500\nNIT No: 2026/45")
    assert label == "Tender Fee"


def test_tender_fee_excludes_emd_alone():
    """EMD/'Earnest Money Deposit'/'Bid Security' alone must not be treated
    as tender-fee evidence - real tender notices list it as a distinct,
    typically refundable line item, not an expense (category_vocabulary.json
    exclusion note)."""
    label, _ = bill_parser._find_category("Bid Security (EMD) (DD/BC/BG in original): Rs 50000")
    assert label != "Tender Fee"


# ---------- TRAVEL-OTHERS ----------


def test_travel_others_positive_visa_fee():
    label, _ = bill_parser._find_category("VFS Global\nVisa Fee Receipt\nApplication Number: 55213")
    assert label == "Travel Miscellaneous"


def test_travel_others_positive_travel_insurance():
    label, _ = bill_parser._find_category("Bajaj Allianz\nCorporate Travel Insurance\nOverseas Mediclaim Policy")
    assert label == "Travel Miscellaneous"


def test_travel_others_excludes_bare_visa_card_brand():
    """Bare 'VISA' is the ubiquitous payment-card network brand printed on
    nearly every card-swipe POS receipt - must never be read as visa-fee
    evidence without an anchoring phrase."""
    label, _ = bill_parser._find_category("VISA DEBIT CARD ENDING 4521\nPOS Transaction Approved\nAmount: 800")
    assert label != "Travel Miscellaneous"


def test_travel_others_excludes_bare_parking():
    label, _ = bill_parser._find_category("Mall Parking Receipt\nAmount: 50\nValid 4 hours")
    assert label != "Travel Miscellaneous"


def test_travel_others_excludes_bare_insurance():
    label, _ = bill_parser._find_category("Health Insurance Premium Receipt\nPolicy No: HI-88213")
    assert label != "Travel Miscellaneous"


# ---------- MARKETING - SEMINAR EXPS ----------


def test_seminar_positive_common_terminology():
    label, _ = bill_parser._find_category("Conference Registration Fee\nDelegate Kit Included\nAmount: 5000")
    assert label == "Seminar/Conference"


def test_seminar_positive_alternate_terminology():
    label, _ = bill_parser._find_category("Exhibition Stall Charges\nShell Scheme\nSpace Rent: 135000")
    assert label == "Seminar/Conference"


def test_seminar_excludes_bare_registration():
    """Bare 'REGISTRATION' alone is too weak (GST/vehicle/company
    registration) - require co-occurrence with SEMINAR/CONFERENCE/DELEGATE/
    WORKSHOP/EXHIBITION (category_vocabulary.json exclusion note)."""
    label, _ = bill_parser._find_category("Company Registration Certificate\nRegistration No: U12345")
    assert label != "Seminar/Conference"


# ---------- MARKETING EXPENSES - OTHERS ----------


def test_marketing_collateral_positive_common_terminology():
    label, _ = bill_parser._find_category("Flex Banner Printing\nHoarding Charges: 15000")
    assert label == "Marketing Collateral"


def test_marketing_collateral_positive_alternate_terminology():
    label, _ = bill_parser._find_category("Corporate Gift Invoice\nBranded Merchandise\nSponsorship Agreement")
    assert label == "Marketing Collateral"


def test_marketing_collateral_excludes_plain_printing():
    """Plain 'PRINTING'/'STATIONERY' alone belongs to PRINTING & STATIONERY,
    not marketing collateral, unless accompanied by marketing-specific
    context (brochure/flyer/banner/flex/hoarding)."""
    label, _ = bill_parser._find_category("Office Printing Services\nPhotocopy Charges\nStationery Items")
    assert label == "Printing & Stationery"


# ---------- SERVICE PURCHASES/SERVICE CHARGE (deliberate fallback-only) ----------


def test_service_purchase_matches_as_last_resort():
    label, _ = bill_parser._find_category("Annual Maintenance Contract\nService Charges for Q1: 8000")
    assert label == "Service Purchase"


def test_service_purchase_never_overrides_a_more_specific_category():
    """A more specific category's own pattern must always win, since
    research found no vocabulary distinguishing this catch-all from most of
    the others - Service Purchase is checked last in CATEGORY_KEYWORDS."""
    label, _ = bill_parser._find_category("Laptop Servicing\nAnnual Maintenance Contract\nMotherboard Repair")
    assert label == "Computer Repair"

    label, _ = bill_parser._find_category("NABL Accredited Calibration Service\nAMC for Instruments")
    assert label == "Calibration"


# ---------- Cross-category confusion pairs explicitly called out in the brief ----------


def test_confusion_cab_charges_vs_local_conveyance_share_evidence_by_design():
    """CAB CHARGES and LOCAL CONVEYANCE intentionally share the same
    Cab/Taxi bill-evidence label (both are ground-passenger-transport
    categories) - this is pre-existing, unchanged behavior, confirmed still
    correct."""
    assert category_map.expected_bill_label("CAB CHARGES") == "Cab/Taxi"
    assert category_map.expected_bill_label("LOCAL CONVEYANCE") == "Cab/Taxi"


def test_confusion_courier_charges_vs_passenger_transport():
    label, _ = bill_parser._find_category("DTDC Courier\nAWB No: 88213\nConsignment Delivery")
    assert label == "Courier"
    label, _ = bill_parser._find_category("Uber Ride Receipt\nRide Fare: 250")
    assert label == "Cab/Taxi"


def test_confusion_own_car_vs_company_car_petrol_share_evidence_by_design():
    """Both share the Fuel label - the distinction between OWN/COMPANY car is
    a policy-ownership question the bill itself can never answer, so both
    correctly rely on the same fuel-purchase evidence."""
    assert category_map.expected_bill_label("OWN CAR PETROL") == "Fuel"
    assert category_map.expected_bill_label("COMPANY CAR PETROL") == "Fuel"


def test_confusion_internet_charges_vs_telephone_accessories():
    label, _ = bill_parser._find_category("Hathway Broadband\nStatic IP Add-on\nMonthly Bill")
    assert label == "Internet"
    label, _ = bill_parser._find_category("Flipkart Order\nWired Earphones\nMobile Cover")
    assert label != "Internet"


def test_confusion_printing_stationery_vs_computer_repairs():
    label, _ = bill_parser._find_category("Stationery Mart\nPhotocopy and Printing Services")
    assert label == "Printing & Stationery"
    label, _ = bill_parser._find_category("Dell Authorized Service Center\nHardware Repair Invoice")
    assert label == "Computer Repair"


def test_confusion_membership_subscription_vs_internet_charges():
    label, _ = bill_parser._find_category("IEEE Membership Renewal\nAnnual Membership Fee")
    assert label == "Membership"
    label, _ = bill_parser._find_category("Jio Fiber Broadband\nInternet Bill\nStatic IP")
    assert label == "Internet"


def test_confusion_toll_tax_charges_vs_transport_charges():
    label, _ = bill_parser._find_category("Toll Plaza Receipt\nFASTag Deduction\nAmount: 65")
    assert label == "Toll"
    label, _ = bill_parser._find_category("Goods Transport Agency\nFreight Charges\nConsignment Note")
    assert label == "Freight/Transport"


def test_confusion_service_purchases_vs_a_specific_service_category():
    """A calibration-specific invoice must win over the generic Service
    Purchase fallback even though it also mentions 'service'."""
    label, _ = bill_parser._find_category("Calibration Certificate\nTesting and Calibration Service Invoice")
    assert label == "Calibration"
