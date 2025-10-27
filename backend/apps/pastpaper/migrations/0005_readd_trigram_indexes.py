from django.contrib.postgres.indexes import GinIndex
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("pastpaper", "0004_remove_pastpapercomponent_component_content_trgm_and_more"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="pastpapercomponent",
            index=GinIndex(
                fields=["content"],
                name="component_content_trgm",
                opclasses=["gin_trgm_ops"],
            ),
        ),
        migrations.AddIndex(
            model_name="pastpapercomponent",
            index=GinIndex(
                fields=["num_display"],
                name="component_num_trgm",
                opclasses=["gin_trgm_ops"],
            ),
        ),
        migrations.AddIndex(
            model_name="pastpapercomponent",
            index=GinIndex(
                fields=["path_normalized"],
                name="component_path_trgm",
                opclasses=["gin_trgm_ops"],
            ),
        ),
    ]
