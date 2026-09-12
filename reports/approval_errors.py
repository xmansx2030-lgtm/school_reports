from django.core.exceptions import ValidationError


class ApprovalError(ValidationError):
    """Domain error for a forbidden approval state or transition."""
