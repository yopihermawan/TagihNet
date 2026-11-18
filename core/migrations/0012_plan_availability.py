from django.db import migrations, models
from core.migration_operations import AddPlanAvailability


class Migration(migrations.Migration):
    dependencies = [("core", "0011_billing_schedule")]
    operations = [AddPlanAvailability(model_name="plan", name="is_active",
        field=models.BooleanField(default=True, db_index=True))]
