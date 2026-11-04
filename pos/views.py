from django.contrib.auth.decorators import login_required
from django.shortcuts import render


@login_required
def screen(request):
    return render(request, "pos/screen.html")