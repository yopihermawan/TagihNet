"""Keep SQLite plan ID high-water marks when Django rebuilds the table for an added field."""
from django.db import migrations


class AddPlanAvailability(migrations.AddField):
    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        connection = schema_editor.connection
        if connection.vendor != "sqlite":
            return super().database_forwards(app_label, schema_editor, from_state, to_state)
        with connection.cursor() as cursor:
            cursor.execute("SELECT seq FROM sqlite_sequence WHERE name='core_plan'")
            previous = cursor.fetchone()
        super().database_forwards(app_label, schema_editor, from_state, to_state)
        with connection.cursor() as cursor:
            if previous is None:
                cursor.execute("DELETE FROM sqlite_sequence WHERE name='core_plan' AND seq=0")
            else:
                cursor.execute("UPDATE sqlite_sequence SET seq=%s WHERE name='core_plan'", [previous[0]])
                if not cursor.rowcount:
                    cursor.execute("INSERT INTO sqlite_sequence(name,seq) VALUES('core_plan',%s)", [previous[0]])
