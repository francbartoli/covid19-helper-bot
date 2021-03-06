import traceback
from fastapi.responses import JSONResponse

from app.services import capture_message


def error_fallback_action(_, exc: Exception):
    """
        This function catches 500 code exceptions and returns a fallback action for the bot to say.
    """
    capture_message(traceback.print_exception(None, exc, exc.__traceback__))
    return JSONResponse(
        content={
            "actions": {
                "actions": [
                    {
                        "say": "Oops, looks like ~the hive mind can't come to an agreement~ I can fulfill your task; "
                        "let's try again "
                    },
                    {"redirect": "task://menu-description"},
                ]
            }
        }
    )
