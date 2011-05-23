from django.contrib import admin
from MP100.watermarker.models import Watermark

class WatermarkAdmin(admin.ModelAdmin):
    model = Watermark
    list_display = ['name', 'is_active']
    list_filter = ['is_active']
    search_fields = ['name']

admin.site.register(Watermark, WatermarkAdmin)