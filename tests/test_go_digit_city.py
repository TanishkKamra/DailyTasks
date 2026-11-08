"""GO DIGIT city clean-up: swap a noisy AI city for the address-derived one.

Synthetic data only: no customer documents, no network.
"""

import pytest

from core import policy_engine as pe

# Minimal GO DIGIT-style text: the rule-based fallback reads the city from the
# ", <City>- <PIN>" tail of the labelled address block.
GO_DIGIT_TEXT = "Address\n12 Sample Street, Testpur- 110001\nMobile\nXXXXXX0000\n"


@pytest.mark.parametrize(
    "city",
    [
        "Delhi",
        "New Delhi",
        "Noida",
        "Greater Noida",
        "Navi Mumbai",
        "Ahmednagar",
        "Gandhinagar",
        "Srinagar",
        "Port Blair",
        "Vasai Virar",
    ],
)
def test_real_city_names_are_not_noisy(city):
    assert not pe._looks_like_noisy_city(city)


@pytest.mark.parametrize(
    "city",
    [
        "NEAR WATER TANKI",
        "PANI KI TANKI",
        "Opp. Bus Stand",
        "BEHIND SAMPLE MARKET",
        "SAMPLE VIHAR",
        "TEST COLONY",
        "Sector 21",
        "H.No 12",
        "Main Road",
        "Block C, Sample Enclave",
    ],
)
def test_landmark_and_address_fragments_are_noisy(city):
    assert pe._looks_like_noisy_city(city)


def test_noisy_ai_city_is_replaced_by_address_city():
    result = pe._backfill_missing_fields_from_text(
        {"city": "NEAR WATER TANKI"}, GO_DIGIT_TEXT, company_mode="GO DIGIT"
    )
    assert result["city"] == "Testpur"


def test_clean_ai_city_is_kept():
    result = pe._backfill_missing_fields_from_text(
        {"city": "Noida"}, GO_DIGIT_TEXT, company_mode="GO DIGIT"
    )
    assert result["city"] == "Noida"
