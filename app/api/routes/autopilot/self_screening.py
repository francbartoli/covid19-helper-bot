import json
from typing import List, Dict
from itertools import chain

import faunadb.errors
from loguru import logger
from requests.exceptions import BaseHTTPError
from fastapi import APIRouter, Form

from app.core import config
from app.custom_router import AutopilotRoute
from app.models import (
    UserDocument,
    screening_not_in_danger,
    screening_pre_results_warning,
)
from app.services import capture_message, NovelCOVIDApi
from app.services.endless_medical_api import EndlessMedicalAPI
from app.utils import features_mapping, reponse_mappings, outcomes_mapping

self_screening = APIRouter()
self_screening.route_class = AutopilotRoute
endless_medical_api = EndlessMedicalAPI(config)
novelcovid_api = NovelCOVIDApi(config)


@self_screening.post("/self-screening/start")
def self_screening_start(UserIdentifier: str = Form(...), Memory: str = Form(...)):
    """
    Starts the screening process
    :param: UserIdentifier: Phone number from Twilio
    :param: Memory: JSON Stringified object from Twilio
    """
    memory = json.loads(Memory)
    twilio = memory.pop("twilio")

    start_screening = twilio["collected_data"]["accepts-test"]["answers"][
        "start-screening"
    ]["answer"]
    user = UserDocument.get_by_phone(UserIdentifier)

    try:
        if start_screening == "No":
            return {
                "actions": [
                    {"say": "Ok no problem! Let's go back to the menu"},
                    {"redirect": "task://menu-description"},
                ]
            }

        # Check if user is in db, otherwise we cannot continue
        if start_screening == "Yes" and not user:
            return {
                "actions": [
                    {
                        "say": "Sorry, I cannot continue until you give me your name \U00012639"
                    },
                    {"redirect": "task://can-have-name"},
                ]
            }
        # Check if the user lives in a COVID-19 affected area
        lives_in_risky_area = novelcovid_api.lives_in_risky_zone(user.country)
        # Said yes but is not in risky area
        if start_screening == "Yes" and not lives_in_risky_area:
            return screening_not_in_danger
        # Said yes and IS in risky area, so we can mark the first question as "yes"
        if start_screening == "Yes" and lives_in_risky_area:
            accept_tos_and_store_session_id(UserIdentifier)

            return {
                "actions": [
                    {
                        "say": "Your country already has cases of COVID-19, so we will skip that question; let's go "
                        "with the rest "
                    },
                    {"redirect": "task://self-screening-q-rest"},
                ]
            }
        # Continue if user accepts
        if start_screening == "Yes":
            return {
                "actions": [
                    {"say": "Alright, let's start"},
                    {"redirect": "task://self-screening-lives-in-area"},
                ]
            }
        return {
            "actions": [
                {"say": "Cool! No problem, let's get back to the menu"},
                {"redirect": "task://menu-description"},
            ]
        }
    except faunadb.errors.BadRequest as err:
        capture_message(err)
        return {"actions": [{"redirect": "task://fallback"}]}
    except BaseHTTPError as err:
        capture_message(err)
        return {"actions": [{"redirect": "task://fallback"}]}
    except Exception as err:
        capture_message(err)
        return {"actions": [{"redirect": "task://fallback"}]}


@self_screening.post("/self-screening/lives-in-area")
def self_screening_lives_in_area(
    UserIdentifier: str = Form(...), Memory: str = Form(...)
):
    """
    Checks if the user lives in a COVID-19 affected area, leaved here for legacy reasons; probably
    does not make sense anymore due to above implementation
    :param: UserIdentifier: Phone number from Twilio
    :param: Memory: JSON Stringified object from Twilio
    """
    memory = json.loads(Memory)
    twilio = memory.pop("twilio")

    start_screening = twilio["collected_data"]["q1"]["answers"]["lives-in-area"][
        "answer"
    ]

    if start_screening == "No":
        return screening_not_in_danger
    try:
        accept_tos_and_store_session_id(UserIdentifier)

        return {"actions": [{"redirect": "task://self-screening-q-rest"}]}
    except faunadb.errors.BadRequest as err:
        capture_message(err)
        return {"actions": [{"redirect": "task://fallback"}]}
    except BaseHTTPError as err:
        capture_message(err)
        return {"actions": [{"redirect": "task://fallback"}]}


@self_screening.post("/self-screening/analyze-answers")
def analyze_answers(UserIdentifier: str = Form(...)):
    """
    Analyzes the answers given by the user, see that we are not parsing the Memory object,
    that's because on every questions the answers were validated by the endpoint defined below
    and added to the Endless Medical API session
    """
    # We don't need to access collect since arriving here means
    # all answers have been added to the Endless Medical Session
    try:
        session_id = UserDocument.get_by_phone(UserIdentifier).endless_medical_token
    except AttributeError as err:
        capture_message(err)
        return {"actions": {"redirect": "task://fallback"}}

    # Just an inline function to clean the user
    def reset_session_id_token():
        user = UserDocument.get_by_phone(UserIdentifier)
        user.endless_medical_token = None
        user.update()

    try:
        # Analyse data and get only the posible diseases
        outcomes: List[Dict[str, str]] = endless_medical_api.analyse(session_id)[
            "Diseases"
        ]
        logger.debug(outcomes)
        # Parsing the diseases, the API returns a confidence from 0.0 to 1.0 for every predicted disease
        # if COVID-19 has >= 50 % we recommend calling the doctor; else we check if any other diseases has at least
        # 50 % chance, if so, COVID-19 it's discarded but we recommend calling the doctor anyway

        # Check if COVID-19 passes threshold
        if check_outcomes(outcomes, outcomes_mapping.get("covid-19")):
            return {
                "actions": list(
                    chain(
                        [
                            {
                                "say": "You should seek medical attention as soon as possible"
                            }
                        ],
                        screening_pre_results_warning,
                    )
                )
            }

        # COVID-19 did not pass threshold, check now if any other outcome does
        if check_outcomes(outcomes):
            return {
                "actions": list(
                    chain(
                        [
                            {
                                "say": "You probably do not have COVID-19, but, according to your symptoms, "
                                "you should seek medical attention anyways "
                            }
                        ],
                        self_screening_lives_in_area,
                    )
                )
            }
        # User is probably ok
        return {
            "actions": [
                {
                    "say": "Great news! You don not have anything to worry about 🥳\U0001f973"
                },
                {"redirect": "task://menu-description"},
            ]
        }
    except BaseHTTPError as err:
        capture_message(err)

        return {
            "actions": [
                {
                    "say": "Sorry, our super AI doctor had a problem analysing the results; please try again."
                },
                {"redirect": "task://menu-description"},
            ]
        }
    finally:
        reset_session_id_token()


@self_screening.post("/self-screening/{feature}")
def add_feature(
    feature: str, UserIdentifier: str = Form(...), ValidateFieldAnswer: str = Form(...)
):
    """
    This endpoint gets called when the user answers a question from the self screening collect,
    the path param must match the symptoms on the feature_mappings and the answer must match
    the ones in the response_mapping. If successful, adds the feature to the Endless Medical
    current session.

    :param: feature: that must match the mapping at features_mapping
    :param: ValidateFieldAnswer: answer from the user, must match reponse_mappings to be valid
    """
    session_id = UserDocument.get_by_phone(UserIdentifier).endless_medical_token
    try:
        logger.debug(
            "Adding {feature} with {value}".format(
                feature=features_mapping[feature],
                value=reponse_mappings[ValidateFieldAnswer.lower()],
            )
        )
        res = endless_medical_api.add_feature(
            session_id,
            features_mapping[feature],
            reponse_mappings[ValidateFieldAnswer.lower()],
        )
        logger.debug(res)
        # Result as per the Twilio Docs
        if not res:
            return {"valid": False}
        return {"valid": True}
    except BaseHTTPError as err:
        capture_message(err)
        return {"valid": False}
    except KeyError as err:
        capture_message(err)
        return {"valid": False}


def check_outcomes(outcomes: List[Dict[str, str]], name: str = None) -> bool:
    """
    The following is horrible, but the API response mapping is not... Ideal
    Helper method to check if an outcome (or any outcome) passes threshold
    """

    for outcome in outcomes:
        disease_name = list(outcome.keys())[0]
        confidence = float(list(outcome.values())[0])

        if name and disease_name == name and confidence >= config.OUTCOME_THRESHOLD:
            return True
        elif not name and confidence >= config.OUTCOME_THRESHOLD:
            return True
    return False


def accept_tos_and_store_session_id(phone_number: str):
    """
    Helper function that:
    1) Requests a session ID from Endless MEdical
    2) Accepts the Endless Medical TOS
    3) Adds the first Feature to the session
    """
    endless_medical_session_id = endless_medical_api.get_session_token()
    if not endless_medical_api.accept_tos(endless_medical_session_id):
        raise BaseHTTPError("Error accepting Endless Medical TOS")
    user = UserDocument.get_by_phone(phone_number)
    user.endless_medical_token = endless_medical_session_id
    logger.debug(
        "Adding {feature} with value {value}".format(
            feature=features_mapping.get("lives-in-area"), value=5
        )
    )
    # 5 is the endless medical api value for "lives in a covid 19 affected area" we assume so because
    # to get into this step the user must have answered yes to this question in a previous step
    # the options was not added to the mapping since overlaps with the "severe" answer mapping
    endless_medical_api.add_feature(
        endless_medical_session_id, features_mapping.get("lives-in-area"), 5
    )
    user.update()
