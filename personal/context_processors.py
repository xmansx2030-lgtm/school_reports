from .services import personal_year_context


def academic_year_navigation(request):
    return personal_year_context(request)
