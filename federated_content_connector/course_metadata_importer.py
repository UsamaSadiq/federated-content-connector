"""Course metadata importer."""

import logging
from urllib.parse import quote_plus, urlencode, urljoin

import backoff
from common.djangoapps.course_modes.models import CourseMode
from django.contrib.auth import get_user_model
from openedx.core.djangoapps.catalog.models import CatalogIntegration
from openedx.core.djangoapps.catalog.utils import get_catalog_api_base_url, get_catalog_api_client
from openedx.core.djangoapps.content.course_overviews.models import CourseOverview

from federated_content_connector.constants import BOOTCAMP_2U, EXEC_ED_COURSE_TYPE
from federated_content_connector.models import CourseDetails

BEST_MODE_ORDER = [
    CourseMode.VERIFIED,
    CourseMode.PROFESSIONAL,
    CourseMode.NO_ID_PROFESSIONAL_MODE,
    CourseMode.UNPAID_EXECUTIVE_EDUCATION,
    CourseMode.AUDIT,
]

logger = logging.getLogger(__name__)
User = get_user_model()


class CourseMetadataImporter:
    """
    Import course metadata from discovery.
    """

    @classmethod
    def get_api_client(cls):
        """
        Return discovery api client.
        """
        catalog_integration = CatalogIntegration.current()
        username = catalog_integration.service_username

        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            logger.exception(
                f'Failed to create API client. Service user {username} does not exist.'
            )
            raise

        return get_catalog_api_client(user)

    @classmethod
    def import_all_courses_metadata(cls):
        """
        Import course metadata for all courses.
        """
        logger.info('[COURSE_METADATA_IMPORTER] Course metadata import started for all courses.')

        all_active_courserun_locators = cls.courserun_locators_to_import()
        cls.import_courses_metadata(all_active_courserun_locators)

        logger.info('[COURSE_METADATA_IMPORTER] Course metadata import completed for all courses.')

    @classmethod
    def import_specific_courses_metadata(cls, courserun_locators):
        """
        Import course metadata for specific courses.

        Args:
            courserun_locators (list): list of courserun locator objects
        """
        logger.info(f'[COURSE_METADATA_IMPORTER] Course metadata import started for courses. {courserun_locators}')

        cls.import_courses_metadata(courserun_locators)

        logger.info(f'[COURSE_METADATA_IMPORTER] Course metadata import completed for courses. {courserun_locators}')

    @classmethod
    def import_courses_metadata(cls, courserun_locators):
        """
        Import course metadata for given course locators.

        Args:
            courserun_locators (list): list of courserun locator objects
        """
        logger.info('[COURSE_METADATA_IMPORTER] Course metadata import started.')

        for courserun_locators_chunk in cls.chunks(courserun_locators):

            client = cls.get_api_client()

            # convert course locator objects to courserun keys
            courserun_keys = list(map(str, courserun_locators_chunk))

            logger.info(f'[COURSE_METADATA_IMPORTER] Importing metadata. Courses: {courserun_keys}')

            course_details, courserun_with_course_uuids = cls.fetch_courses_details(
                client,
                courserun_locators_chunk,
                get_catalog_api_base_url()
            )
            processed_courses_details = cls.process_courses_details(course_details, courserun_with_course_uuids)
            cls.store_courses_details(processed_courses_details)

            logger.info(f'[COURSE_METADATA_IMPORTER] Import completed. Courses: {courserun_keys}')

        logger.info('[COURSE_METADATA_IMPORTER] Course metadata import completed for all courses.')

    @classmethod
    def courserun_locators_to_import(cls):
        """
        Construct list of all course locators for which we want to import data.
        """
        return list(CourseOverview.objects.all().values_list('id', flat=True))

    @classmethod
    def fetch_courses_details(cls, client, courserun_locators, api_base_url):
        """
        Fetch the course data from discovery using `/api/v1/courses` endpoint.
        """
        courserun_with_course_uuids = cls.fetch_course_uuids(client, api_base_url, courserun_locators)
        course_uuids = courserun_with_course_uuids.values()
        course_uuids_str = ','.join(course_uuids)

        logger.info(f'[COURSE_METADATA_IMPORTER] Fetching details from discovery. Course UUIDs {course_uuids}.')
        api_url = urljoin(
            f"{api_base_url}/", f"courses/?limit=50&include_hidden_course_runs=1&uuids={course_uuids_str}"
        )
        response = cls.get_response_from_api(client, api_url)
        response.raise_for_status()
        courses_details = response.json()
        results = courses_details.get('results', [])

        return results, courserun_with_course_uuids

    @classmethod
    def fetch_course_uuids(cls, client, api_base_url, courserun_locators):
        """
        Return a map of courserun key and course uuid.
        """
        courserun_keys = list(map(str, courserun_locators))
        encoded_courserun_keys = ','.join(map(quote_plus, courserun_keys))

        logger.info(f'[COURSE_METADATA_IMPORTER] Fetching uuids for Courseruns {encoded_courserun_keys}')
        api_url = urljoin(
            f"{api_base_url}/", f"course_runs/?limit=50&include_hidden_course_runs=1&keys={encoded_courserun_keys}"
        )
        response = cls.get_response_from_api(client, api_url)
        response.raise_for_status()
        courses_details = response.json()
        results = courses_details.get('results', [])

        courserun_with_course_uuids = {}
        for result in results:
            courserun_key = result.get('key')

            if courserun_key not in courserun_keys:
                continue

            courserun_with_course_uuids[courserun_key] = result.get('course_uuid')

        return courserun_with_course_uuids

    @classmethod
    def courses(cls, timestamp):
        """Fetch courses updated since `timestamp`."""
        query_params = {
            'timestamp': timestamp,
            'limit': 50,
            'include_hidden_course_runs': 1,
        }
        client = cls.get_api_client()
        api_base_url = get_catalog_api_base_url()
        params = urlencode(query_params)
        api_url = urljoin(f"{api_base_url}/", f"courses/?{params}")
        results, next_url, total = cls.get_api_reponse(client, api_url)
        logger.info(f'[COURSE_METADATA_IMPORTER] Total Records are {total}')
        yield results

        while next_url:
            results, next_url, __ = cls.get_api_reponse(client, next_url)
            yield results

    @classmethod
    def get_api_reponse(cls, client, api_url):
        """Get response from API."""
        response = cls.get_response_from_api(client, api_url)
        response.raise_for_status()
        courses = response.json()
        results = courses.get('results', [])
        return results, courses.get('next'), courses.get('count')

    @classmethod
    @backoff.on_exception(
        backoff.expo,
        Exception,
        max_tries=3,
        logger=logger,
    )
    def get_response_from_api(cls, client, api_url):
        """
        Call api endpoint and return response.
        """
        return client.get(api_url)

    @classmethod
    def process_courses_details(cls, courses_details, courserun_with_course_uuids):
        """
        Parse and extract the minimal data that we need.
        """
        log_prefix = 'COURSE_METADATA_IMPORTER'

        courses = {}
        for courserun_key, course_uuid in courserun_with_course_uuids.items():
            logger.info(f'[{log_prefix}] Process. CourserunKey: {courserun_key}, CourseUUID: {course_uuid}')
            course_metadata = cls.find_attr(courses_details, 'uuid', course_uuid)
            if not course_metadata:
                logger.info(f'[COURSE_METADATA_IMPORTER] Metadata not found. CourseUUID: {course_uuid}')
                continue

            course_type = course_metadata.get('course_type') or ''
            product_source = course_metadata.get('product_source') or ''
            if product_source:
                product_source = product_source.get('slug')

            enroll_by = start_date = end_date = None

            if course_type in (EXEC_ED_COURSE_TYPE, BOOTCAMP_2U):
                additional_metadata = course_metadata.get('additional_metadata')
                if additional_metadata:
                    enroll_by = additional_metadata.get('registration_deadline')
                    start_date = additional_metadata.get('start_date')
                    end_date = additional_metadata.get('end_date')
            else:
                course_run = cls.find_attr(course_metadata.get('course_runs'), 'key', courserun_key)
                if course_run:
                    seat = cls.find_best_mode_seat(course_run.get('seats'))
                    if seat:
                        enroll_by = seat.get('upgrade_deadline')
                    else:
                        logger.info(
                            f"[{log_prefix}] No Seat Found. Seats: {course_run.get('seats')}"
                        )
                    start_date = course_run.get('start')
                    end_date = course_run.get('end')
                else:
                    logger.info(
                        f'[{log_prefix}] Courserun not found. CourserunKey: {courserun_key}, CourseUUID: {course_uuid}'
                    )
                    continue

            course_data = {
                'course_type': course_type,
                'product_source': product_source,
                'enroll_by': enroll_by,
                'start_date': start_date,
                'end_date': end_date,
            }
            courses[courserun_key] = course_data

        return courses

    @classmethod
    def store_courses_details(cls, courses_details):
        """
        Store courses metadata in database.
        """
        for courserun_key, course_detail in courses_details.items():
            CourseDetails.objects.update_or_create(
                id=courserun_key,
                defaults=course_detail
            )

    @classmethod
    def find_best_mode_seat(cls, seats):
        """
        Find the seat by best course mode.
        """
        def sort_key(mode):
            """
            Assign a weight to the seat dictionary based on the position of its type in best moode order list.
            """
            mode_type = mode['type']
            if mode_type in BEST_MODE_ORDER:
                return len(BEST_MODE_ORDER) - BEST_MODE_ORDER.index(mode_type)
            else:
                return 0

        sorted_seats = sorted(seats, key=sort_key, reverse=True)
        if sorted_seats:
            return sorted_seats[0]

        return None

    @classmethod
    def chunks(cls, keys, chunk_size=50):
        """
        Yield chunks of size `chunk_size`.
        """
        for i in range(0, len(keys), chunk_size):
            yield keys[i:i + chunk_size]

    @staticmethod
    def construct_course_key(course_locator):
        """
        Construct course key from course run key.
        """
        return f'{course_locator.org}+{course_locator.course}'

    @classmethod
    def find_attr(cls, iterable, attr_name, attr_value):
        """
        Find value of an attribute from with in an iterable.
        """
        for item in iterable:
            if item[attr_name] == attr_value:
                return item

        return None
