import logging

from librespot.core import ApResolver, Session
from librespot.mercury import MercuryClient
from librespot.proto import Authentication_pb2 as Authentication

logger = logging.getLogger(__name__)


class Librespot:
    def __init__(
        self,
        access_token: str,
    ) -> None:
        self.access_token = access_token

        self._initialize()

    def _initialize(self) -> None:
        login_credentials = Authentication.LoginCredentials(
            username=None,
            typ=Authentication.AuthenticationType.AUTHENTICATION_SPOTIFY_TOKEN,
            auth_data=self.access_token.encode(),
        )

        builder = Session.Builder()
        builder.login_credentials = login_credentials

        builder.conf = (
            Session.Configuration.Builder()
            .set_store_credentials(False)
            .set_cache_enabled(False)
            .build()
        )

        last_error = None
        for attempt in range(1, 4):
            self.session = self._build_session(builder)

            try:
                self.session.connect()
                self.session.authenticate(login_credentials)
                return
            except MercuryClient.MercuryException as exc:
                if exc.code != 403:
                    last_error = exc
                else:
                    # Session.authenticate() initializes the token/api/audio-key
                    # managers before the dealer websocket connects. Keep the
                    # partial session when only the dealer connection is denied.
                    logger.warning(
                        "Librespot dealer connection returned 403; "
                        "continuing with the partially initialized session"
                    )
                    return
            except Exception as exc:
                last_error = exc

            try:
                self.session.close()
            except Exception:
                pass

            logger.warning(
                "Librespot session initialization attempt %s failed: %s",
                attempt,
                last_error,
            )

        if last_error is None:
            raise RuntimeError("Librespot session initialization failed")

        raise last_error

    @staticmethod
    def _build_session(builder: Session.Builder) -> Session:
        return Session(
            Session.Inner(
                builder.device_type,
                builder.device_name,
                builder.preferred_locale,
                builder.conf,
                builder.device_id,
            ),
            ApResolver.get_random_accesspoint(),
        )
