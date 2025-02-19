from datetime import datetime, timedelta
from typing import List, Tuple, Dict, Optional
from pocketflow import Node, Flow, BatchFlow
import yaml
import logging
import time
import email
import email.utils

# Configure logging with more detailed format
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

from utils.call_llm import call_llm
from utils.check_unread_emails import check_unread_emails
from utils.check_availability import check_availability
from utils.schedule_meeting import schedule_meeting
from utils.send_email import send_email

class EmailFetcherNode(Node):
    """Monitors inbox for new emails from authorized user."""
    def prep(self, shared):
        logger.debug("Preparing email fetcher with config from shared state")
        return {
            "username": shared["config"]["email"]["username"],
            "password": shared["config"]["email"]["password"],
            "authorized_user": shared["config"]["authorized_user"]
        }
        
    def exec(self, config):
        logger.info("Starting email check cycle")
        logger.debug(f"Checking emails for authorized user: {config['authorized_user']}")
        
        emails = check_unread_emails(
            username=config["username"],
            password=config["password"]
        )
        
        if not emails:
            logger.info("No unread emails found")
            return None
            
        logger.info(f"Found {len(emails)} unread emails")
        logger.debug(f"Email subjects: {[e.get('subject', 'No subject') for e in emails]}")
            
        # Parse authorized user email
        authorized_email = email.utils.getaddresses([config["authorized_user"]])[0][1].lower().strip()
        logger.debug(f"Parsed authorized email: {authorized_email}")
            
        # Filter for authorized user
        authorized_emails = [
            e for e in emails 
            if (email.utils.getaddresses([e["sender"]])[0][1].lower().strip() == authorized_email or
                authorized_email in e["cc"] or
                authorized_email in e["bcc"])
        ]
        
        if authorized_emails:
            logger.info(f"Found {len(authorized_emails)} emails involving authorized user")
            for e in authorized_emails:
                logger.debug(f"Authorized email - Subject: {e.get('subject', 'No subject')}, From: {e['sender']}")
        else:
            logger.info("No emails found involving authorized user")
        
        return authorized_emails if authorized_emails else None

    def post(self, shared, prep_res, exec_res):
        if not exec_res:
            logger.info("No emails to process, waiting 30s before next check")
            time.sleep(30)
            return "monitor"
        
        # Reset and store new emails
        shared["pending_emails"] = {
            msg["message_id"]: msg for msg in exec_res
        }
        logger.info(f"Stored {len(shared['pending_emails'])} new emails for processing")
        logger.debug(f"Message IDs: {list(shared['pending_emails'].keys())}")
        return "analyze_batch"

class EmailIntentAnalyzerNode(Node):
    """Determines if email is for scheduling."""
    def prep(self, shared):
        email_id = self.params["email_id"]
        logger.info(f"Analyzing intent of email {email_id}")
        email = shared["pending_emails"][email_id]
        logger.debug(f"Email subject: {email.get('subject', 'No subject')}, From: {email['sender']}")
        return shared["pending_emails"][email_id]["body"], shared["config"]
        
    def exec(self, inputs):
        email_body, config = inputs
        logger.debug("Calling LLM to analyze email intent")
        prompt = f"""You are the AI scheduler for User: {config["authorized_user"]}

Email:
{email_body}

Determine if this email is about scheduling a meeting.

Output in yaml:
```yaml
is_scheduling: true/false
reason: why this classification
```"""
        response = call_llm(prompt)
        yaml_str = response.split("```yaml")[1].split("```")[0].strip()
        result = yaml.safe_load(yaml_str)
        logger.info(f"Email classified as {'scheduling' if result['is_scheduling'] else 'non-scheduling'}")
        logger.debug(f"Classification reason: {result['reason']}")
        return result
        
    def post(self, shared, prep_res, exec_res):
        email_id = self.params["email_id"]
        if exec_res["is_scheduling"]:
            logger.info(f"Email {email_id} is about scheduling, proceeding to extract details")
            return "extract_availability_range"
        logger.info(f"Email {email_id} is not about scheduling, removing from queue")
        del shared["pending_emails"][email_id]
        return "end"

class AvailabilityRangeExtractorNode(Node):
    """Extracts time range and meeting details."""
    def prep(self, shared):
        email_id = self.params["email_id"]
        logger.info(f"Extracting availability range from email {email_id}")
        msg = shared["pending_emails"][email_id]
        logger.debug(f"Email details - Subject: {msg.get('subject', 'No subject')}, From: {msg['sender']}")
        return msg["body"], msg["sender"], msg.get("to", []), msg.get("cc", []), msg.get("bcc", []), shared["config"]
        
    def exec(self, inputs):
        email_body, sender, to_list, cc_list, bcc_list, config = inputs
        logger.debug(f"Processing email from {sender} with {len(to_list)} To, {len(cc_list)} CC and {len(bcc_list)} BCC recipients")
        
        # Calculate default timeframe (next week)
        today = datetime.now()
        this_week_end = today + timedelta(days=(6 - today.weekday()))
        next_monday = today + timedelta(days=(7 - today.weekday()))
        next_friday = next_monday + timedelta(days=4)
        
        logger.debug(f"Default timeframes - This week: until {this_week_end.strftime('%Y-%m-%d')}, Next week: {next_monday.strftime('%Y-%m-%d')} to {next_friday.strftime('%Y-%m-%d')}")
        
        prompt = f"""You are the AI scheduler for User: {config["authorized_user"]}

Today's date: {today.strftime("%Y-%m-%d")}
Current week ends: {this_week_end.strftime("%Y-%m-%d")}
Next week: {next_monday.strftime("%Y-%m-%d")} to {next_friday.strftime("%Y-%m-%d")}

Email participants:
User (I schedule for): {config["authorized_user"]}
Email Account Used: {config["email"]["username"]}
From: {sender}
To: {', '.join(to_list) if to_list else 'None'}
CC: {', '.join(cc_list) if cc_list else 'None'}
BCC: {', '.join(bcc_list) if bcc_list else 'None'}

Email:
{email_body}

Extract meeting details with these rules:
1. For timeframe:
   - If specific times/ranges given (e.g. "Tuesday 2-3pm, Wednesday 1-4pm"):
     - Use earliest time as start
     - Use latest time as end
   - If general timeframe (e.g. "this week"):
     - Use today to end of current week
   - If no timeframe mentioned:
     - Use next week (Monday to Friday)
2. For duration:
   - Use explicitly mentioned duration
   - Default to 30 minutes if not specified
3. For location:
   - Include any meeting room, address, or conference link
   - Leave empty if not specified
4. For description:
   - Include meeting purpose, agenda, and any context
   - Be specific about topics to be discussed
   - Consider the context of all participants relative to {config["authorized_user"]}

Output in yaml:
```yaml
duration: duration in minutes
timeframe:
  start: YYYY-MM-DD HH:MM ET
  end: YYYY-MM-DD HH:MM ET
attendees:
  - email1@example.com
location: optional meeting location/link
description: meeting description/agenda
reason: explanation of how timeframe was determined
```"""
        logger.debug("Calling LLM to extract meeting details")
        response = call_llm(prompt)
        yaml_str = response.split("```yaml")[1].split("```")[0].strip()
        result = yaml.safe_load(yaml_str)
        
        # Validate required fields
        assert isinstance(result, dict), "Result must be a dictionary"
        assert "duration" in result, "Duration is required"
        assert isinstance(result["duration"], int), "Duration must be an integer"
        assert "timeframe" in result, "Timeframe is required"
        assert "start" in result["timeframe"], "Start time is required"
        assert "end" in result["timeframe"], "End time is required"
        assert "description" in result, "Description is required"
        assert "reason" in result, "Reason for timeframe selection is required"
        
        # Convert times to datetime
        try:
            result["timeframe"]["start"] = datetime.strptime(result["timeframe"]["start"], "%Y-%m-%d %H:%M ET")
            result["timeframe"]["end"] = datetime.strptime(result["timeframe"]["end"], "%Y-%m-%d %H:%M ET")
        except ValueError as e:
            logger.error(f"Failed to parse datetime: {e}")
            raise
            
        # Validate timeframe logic
        assert result["timeframe"]["start"] < result["timeframe"]["end"], "Start time must be before end time"
        assert result["timeframe"]["start"] >= today, "Start time cannot be in the past"
        
        # Add all participants
        result.setdefault("attendees", [])
        for addr in [sender] + cc_list + bcc_list:
            email_addr = email.utils.getaddresses([addr])[0][1].lower().strip()
            if email_addr not in result["attendees"]:
                result["attendees"].append(email_addr)
        
        # Set default location if not provided
        result.setdefault("location", "")
        
        logger.info(f"Extracted meeting details - Duration: {result['duration']}min, Timeframe: {result['reason']}")
        logger.debug(f"Meeting timeframe: {result['timeframe']['start']} to {result['timeframe']['end']}")
        logger.debug(f"Meeting description: {result['description']}")
        logger.debug(f"Attendees: {', '.join(result['attendees'])}")
        return result
        
    def post(self, shared, prep_res, exec_res):
        email_id = self.params["email_id"]
        shared["pending_emails"][email_id]["request"] = {
            "status": "pending",
            "meeting": exec_res,
            "available_slots": [],
            "chosen_slot": None
        }
        logger.info(f"Created meeting request for email {email_id}")
        return "check_availability"

class AvailabilityCheckerNode(Node):
    """Checks calendar for available slots."""
    def prep(self, shared):
        email_id = self.params["email_id"]
        request = shared["pending_emails"][email_id]["request"]
        logger.info(f"Checking calendar availability for email {email_id}")
        logger.debug(f"Checking from {request['meeting']['timeframe']['start']} to {request['meeting']['timeframe']['end']}")
        return {
            "start_time": request["meeting"]["timeframe"]["start"],
            "end_time": request["meeting"]["timeframe"]["end"],
            "duration": request["meeting"]["duration"],
            "working_hours": shared["config"]["calendar"]["working_hours"]
        }
        
    def exec(self, inputs):
        logger.debug(f"Searching for {inputs['duration']}min slots between {inputs['start_time']} and {inputs['end_time']}")
        slots = check_availability(
            start_time=inputs["start_time"],
            end_time=inputs["end_time"],
            min_duration=timedelta(minutes=inputs["duration"]),
            working_hours=inputs["working_hours"]
        )
        if slots:
            logger.info(f"Found {len(slots)} available time slots")
            for i, (start, end) in enumerate(slots, 1):
                logger.debug(f"Slot {i}: {start} to {end}")
        else:
            logger.warning("No available time slots found")
        return slots
        
    def post(self, shared, prep_res, exec_res):
        email_id = self.params["email_id"]
        shared["pending_emails"][email_id]["request"]["available_slots"] = exec_res
        logger.info(f"Stored {len(exec_res) if exec_res else 0} available slots for email {email_id}")
        return "decide_next_action"

class ActionDeciderNode(Node):
    """Decides whether to schedule or propose times."""
    def prep(self, shared):
        email_id = self.params["email_id"]
        email = shared["pending_emails"][email_id]
        request = email["request"]
        logger.info(f"Deciding next action for email {email_id}")
        return {
            "email_body": email["body"],
            "available_slots": request["available_slots"],
            "meeting": request["meeting"],
            "config": shared["config"]
        }
        
    def exec(self, inputs):
        logger.debug("Calling LLM to analyze scheduling action")
        # Format slots for LLM
        slots_text = []
        for start, end in inputs["available_slots"]:
            slots_text.append(f"- {start.strftime('%I:%M %p ET on %B %d, %Y')} to {end.strftime('%I:%M %p ET')}")
            
        prompt = f"""You are the AI scheduler for User: {inputs["config"]["authorized_user"]}

Email body:
```email
{inputs['email_body']}
```

Available slots for {inputs["config"]["authorized_user"]}:
{chr(10).join(slots_text)}

Analyze if the email already specifies time preferences that overlap with the available slots.

Consider:
- If the email mentions specific times/preferences that match any available slot
- If the email has clear timing requests we can use to choose a slot
- If we need to ask for preferences because no clear time preference was given

Output in yaml:
```yaml
action: schedule/ask_time
reason: detailed explanation of decision
chosen_slot: null or "YYYY-MM-DD HH:MM ET to HH:MM ET" if scheduling
```"""
        response = call_llm(prompt)
        yaml_str = response.split("```yaml")[1].split("```")[0].strip()
        result = yaml.safe_load(yaml_str)
        
        # Convert chosen_slot to datetime tuple if present
        if result["action"] == "schedule" and result["chosen_slot"]:
            start_str, end_str = result["chosen_slot"].split(" to ")
            start = datetime.strptime(start_str, "%Y-%m-%d %H:%M ET")
            end = datetime.strptime(f"{start_str.split(' ')[0]} {end_str}", "%Y-%m-%d %H:%M ET")
            result["chosen_slot"] = (start, end)
            
        logger.info(f"Decided action: {result['action']}")
        logger.debug(f"Decision reason: {result['reason']}")
        if result["chosen_slot"]:
            logger.debug(f"Chosen slot: {result['chosen_slot']}")
        return result
        
    def post(self, shared, prep_res, exec_res):
        email_id = self.params["email_id"]
        if exec_res["action"] == "schedule":
            shared["pending_emails"][email_id]["request"]["chosen_slot"] = exec_res["chosen_slot"]
            logger.info("Can schedule meeting, proceeding to scheduler")
            return "schedule"
        logger.info("Need to ask for preferences, proceeding to proposal")
        return "ask_time"

class MeetingSchedulerNode(Node):
    """Creates calendar event for chosen time slot."""
    def prep(self, shared):
        email_id = self.params["email_id"]
        request = shared["pending_emails"][email_id]["request"]
        logger.info(f"Preparing to schedule meeting for email {email_id}")
        return {
            "meeting": request["meeting"],
            "chosen_slot": request["chosen_slot"]
        }
        
    def exec(self, inputs):
        start_time, end_time = inputs["chosen_slot"]
        logger.debug(f"Scheduling meeting from {start_time} to {end_time}")
        
        meeting_details = {
            "title": inputs["meeting"]["description"],
            "start_time": start_time,
            "end_time": end_time,
            "description": inputs["meeting"]["description"],
            "attendees": inputs["meeting"]["attendees"],
            "location": inputs["meeting"].get("location", "")
        }
        
        event = schedule_meeting(meeting_details)
        logger.info("Meeting scheduled successfully")
        logger.debug(f"Event link: {event.get('htmlLink')}")
        return event
        
    def post(self, shared, prep_res, exec_res):
        email_id = self.params["email_id"]
        shared["pending_emails"][email_id]["request"]["status"] = "scheduled"
        shared["pending_emails"][email_id]["request"]["event"] = exec_res
        return "send_confirmation"

class ScheduleConfirmationEmailNode(Node):
    """Drafts confirmation email for scheduled meeting."""
    def prep(self, shared):
        email_id = self.params["email_id"]
        email = shared["pending_emails"][email_id]
        request = email["request"]
        logger.info(f"Preparing confirmation email for {email_id}")
        
        return {
            "meeting": request["meeting"],
            "event": request["event"],
            "config": shared["config"],
            "threading": {
                "message_id": email["message_id"],
                "references": email.get("references", []),
                "subject": email.get("subject", "Meeting Coordination")
            },
            "original_subject": email.get("subject", "Meeting Coordination"),
            "sender": email["sender"],
            "to": email.get("to", []),
            "cc": email.get("cc", []),
            "bcc": email.get("bcc", [])
        }
        
    def exec(self, inputs):
        start_time = datetime.fromisoformat(inputs["event"]["start"]["dateTime"])
        end_time = datetime.fromisoformat(inputs["event"]["end"]["dateTime"])
        
        prompt = f"""You are the AI scheduler for User: {inputs["config"]["authorized_user"]}


Original email:
- From: {inputs['sender']}
- To: {', '.join(inputs['to'])}
- CC: {', '.join(inputs['cc'])}
- BCC: {', '.join(inputs['bcc'])}
- Subject: {inputs['original_subject']}

Draft a confirmation email for a {inputs['meeting']['duration']}-minute meeting.
The meeting is scheduled for {start_time.strftime('%I:%M %p ET on %B %d, %Y')} to {end_time.strftime('%I:%M %p ET')}.

Calendar link: {inputs['event'].get('htmlLink')}

Draft a brief email that:
1. Confirms the scheduled time
2. Includes the calendar link
3. Keep it short and professional
4. Consider the context of all recipients relative to {inputs["config"]["authorized_user"]}

Output in yaml:
```yaml
body: |
    email body
```"""
        logger.debug("Calling LLM to draft confirmation email")
        response = call_llm(prompt)
        yaml_str = response.split("```yaml")[1].split("```")[0].strip()
        email_content = yaml.safe_load(yaml_str)
        return email_content
        
    def post(self, shared, prep_res, exec_res):
        email_id = self.params["email_id"]
        shared["pending_emails"][email_id]["draft_email"] = {
            "subject": shared["pending_emails"][email_id].get("subject", "Meeting Coordination"),
            "body": exec_res["body"]
        }
        return "send_email"

class ProposalEmailNode(Node):
    """Drafts and sends email with available slots."""
    def prep(self, shared):
        email_id = self.params["email_id"]
        email = shared["pending_emails"][email_id]
        request = email["request"]
        logger.info(f"Preparing to send proposal email for {email_id}")
        
        return {
            "slots": request["available_slots"],
            "meeting": request["meeting"],
            "config": shared["config"],
            "threading": {
                "message_id": email["message_id"],
                "references": email.get("references", [])
            },
            "original_subject": email.get("subject", "Meeting Coordination"),
            "sender": email["sender"],
            "to": email.get("to", []),
            "cc": email.get("cc", []),
            "bcc": email.get("bcc", [])
        }
        
    def exec(self, inputs):
        logger.debug(f"Drafting proposal email with {len(inputs['slots'])} time slots")
        # Format slots for email
        slots_text = []
        for start, end in inputs["slots"]:
            slots_text.append(f"- {start.strftime('%I:%M %p ET on %B %d, %Y')} to {end.strftime('%I:%M %p ET')}")
            
        prompt = f"""You are the AI scheduler for User: {inputs["config"]["authorized_user"]}

Original email:
- From: {inputs['sender']}
- To: {', '.join(inputs['to'])}
- CC: {', '.join(inputs['cc'])}
- BCC: {', '.join(inputs['bcc'])}
- Subject: {inputs['original_subject']}

Draft a concise email proposing times for a {inputs['meeting']['duration']}-minute meeting.

Available slots:
{chr(10).join(slots_text)}

Draft a brief email that:
1. Lists the available time slots
2. Asks recipients to choose their preferred time
3. Keep it short and professional
4. Consider the context of all recipients relative to {inputs["config"]["authorized_user"]}

Output in yaml:
```yaml
body: |
    email body
```"""
        logger.debug("Calling LLM to draft proposal email")
        response = call_llm(prompt)
        yaml_str = response.split("```yaml")[1].split("```")[0].strip()
        email_content = yaml.safe_load(yaml_str)
        
        return email_content
        
    def post(self, shared, prep_res, exec_res):
        email_id = self.params["email_id"]
        shared["pending_emails"][email_id]["draft_email"] = {
            "subject": shared["pending_emails"][email_id].get("subject", "Meeting Coordination"),
            "body": exec_res["body"]
        }
        return "send_email"

class NoSlotsEmailNode(Node):
    """Sends email when no slots are available."""
    def prep(self, shared):
        email_id = self.params["email_id"]
        email = shared["pending_emails"][email_id]
        request = email["request"]
        logger.info(f"Preparing to send no-slots email for {email_id}")
        
        return {
            "meeting": request["meeting"],
            "config": shared["config"],
            "threading": {
                "message_id": email["message_id"],
                "references": email.get("references", [])
            },
            "original_subject": email.get("subject", "Meeting Coordination"),
            "sender": email["sender"],
            "to": email.get("to", []),
            "cc": email.get("cc", []),
            "bcc": email.get("bcc", [])
        }
        
    def exec(self, inputs):
        logger.debug("Drafting no-slots email")
        prompt = f"""You are the AI scheduler for User: {inputs["config"]["authorized_user"]}

Original email:
- From: {inputs['sender']}
- To: {', '.join(inputs['to'])}
- CC: {', '.join(inputs['cc'])}
- BCC: {', '.join(inputs['bcc'])}
- Subject: {inputs['original_subject']}

Draft an email explaining no slots are available for a {inputs['meeting']['duration']}-minute meeting.

Draft a brief email that:
1. Explains no suitable times were found
2. Suggests trying a different week/timeframe
3. Apologizes for the inconvenience
4. Consider the context of all recipients relative to {inputs["config"]["authorized_user"]}

Output in yaml:
```yaml
body: |
    email body
```"""
        logger.debug("Calling LLM to draft no-slots email")
        response = call_llm(prompt)
        yaml_str = response.split("```yaml")[1].split("```")[0].strip()
        email_content = yaml.safe_load(yaml_str)
        return email_content
        
    def post(self, shared, prep_res, exec_res):
        email_id = self.params["email_id"]
        shared["pending_emails"][email_id]["draft_email"] = {
            "subject": shared["pending_emails"][email_id].get("subject", "Meeting Coordination"),
            "body": exec_res["body"]
        }
        return "send_email"

class EmailSenderNode(Node):
    """Sends drafted email with proper threading."""
    def prep(self, shared):
        email_id = self.params["email_id"]
        email = shared["pending_emails"][email_id]
        draft = email["draft_email"]
        logger.info(f"Preparing to send email for {email_id}")
        
        return {
            "draft": draft,
            "meeting": email["request"]["meeting"],
            "email_config": shared["config"]["email"],
            "threading": {
                "message_id": email["message_id"],
                "references": email.get("references", [])
            }
        }
        
    def exec(self, inputs):
        logger.debug(f"Sending email with subject: {inputs['draft']['subject']}")
        
        send_email(
            subject=inputs["draft"]["subject"],
            body=inputs["draft"]["body"],
            to_emails=inputs["meeting"]["attendees"],
            from_email=inputs["email_config"]["username"],
            app_password=inputs["email_config"]["password"],
            in_reply_to=inputs["threading"]["message_id"],
            references=inputs["threading"]["references"]
        )
        logger.info("Email sent successfully")
        return True
        
    def post(self, shared, prep_res, exec_res):
        return "end"

class EmailAnalysisBatchFlow(BatchFlow):
    """Batch processes multiple emails."""
    def prep(self, shared):
        logger.info(f"Starting batch analysis of {len(shared['pending_emails'])} emails")
        return [{"email_id": email_id} for email_id in shared["pending_emails"].keys()]

# Create nodes
email_fetcher = EmailFetcherNode()
email_analyzer = EmailIntentAnalyzerNode()
range_extractor = AvailabilityRangeExtractorNode()
availability_checker = AvailabilityCheckerNode()
action_decider = ActionDeciderNode()
meeting_scheduler = MeetingSchedulerNode()
schedule_confirmation = ScheduleConfirmationEmailNode()
time_proposal = ProposalEmailNode()
no_slots = NoSlotsEmailNode()
email_sender = EmailSenderNode()

# Connect nodes in the batch flow
email_analyzer - "extract_availability_range" >> range_extractor
email_analyzer - "end" >> None

range_extractor - "check_availability" >> availability_checker
availability_checker - "decide_next_action" >> action_decider

action_decider - "schedule" >> meeting_scheduler
action_decider - "ask_time" >> time_proposal

meeting_scheduler - "send_confirmation" >> schedule_confirmation
schedule_confirmation - "send_email" >> email_sender
time_proposal - "send_email" >> email_sender

email_sender - "end" >> None

# Create batch flow
email_analysis = EmailAnalysisBatchFlow(start=email_analyzer)

# Connect main flow
email_fetcher - "monitor" >> email_fetcher
email_fetcher - "analyze_batch" >> email_analysis
email_analysis - "default" >> email_fetcher

# Create main flow
scheduler_flow = Flow(start=email_fetcher)

