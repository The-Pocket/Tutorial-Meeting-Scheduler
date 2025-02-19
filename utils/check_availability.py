import datetime
import zoneinfo  # For timezone handling
from google.oauth2 import service_account
from googleapiclient.discovery import build
from typing import List, Tuple

def check_availability(start_time: datetime.datetime, 
                      end_time: datetime.datetime,
                      calendar_id: str = 'primary',
                      min_duration: datetime.timedelta = datetime.timedelta(minutes=30),
                      working_hours: Tuple[int, int] = (9, 17)) -> List[Tuple[datetime.datetime, datetime.datetime]]:
    pass
