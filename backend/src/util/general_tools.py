from pydantic import BaseModel, Field


class ToSearchAgent(BaseModel):
    """Record the customer's requirement details and search our listings for matches."""

    request: str = Field(description="The customer's requirement details in their own words.")


class ToAppointmentAgent(BaseModel):
    """Hand over to the appointment specialist to book, change, cancel or list site visits and video walkthroughs."""

    request: str = Field(description="What the customer wants, including any day or time they mentioned.")


class CompleteOrEscalate(BaseModel):
    """Mark the current task as complete, or hand the conversation back to the main assistant
    when the customer's request is outside this specialist's job."""

    cancel: bool = True
    reason: str
