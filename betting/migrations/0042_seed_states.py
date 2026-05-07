from django.db import migrations


def seed_states(apps, schema_editor):
    State = apps.get_model('betting', 'State')

    states = [
        ("Abia", "Abia"),
        ("Abuja Federal Capital Territory", "Abuja"),
        ("Adamawa", "Adam"),
        ("Akwa Ibom", "Akw"),
        ("Anambra", "Anam"),
        ("Bauchi", "Bau"),
        ("Bayelsa", "Bay"),
        ("Benue", "Ben"),
        ("Borno", "Borno"),
        ("Cross River", "Cross"),
        ("Delta", "Delta"),
        ("Ebonyi", "Ebon"),
        ("Edo", "Edo"),
        ("Ekiti", "Ekit"),
        ("Enugu", "Enu"),
        ("Gombe", "Gom"),
        ("Imo", "Imo"),
        ("Jigawa", "Jig"),
        ("Kaduna", "Kad"),
        ("Kano", "Kan"),
        ("Katsina", "Kat"),
        ("Kebbi", "Keb"),
        ("Kogi", "Kog"),
        ("Kwara", "Kwara"),
        ("Lagos", "Lag"),
        ("Nasarawa", "Nas"),
        ("Niger", "Niger"),
        ("Ogun", "Ogun"),
        ("Ondo", "Ondo"),
        ("Osun", "Osun"),
        ("Oyo", "Oyo"),
        ("Plateau", "Plat"),
        ("Rivers", "Rivers"),
        ("Sokoto", "Soko"),
        ("Taraba", "Tar"),
        ("Yobe", "Yobe"),
        ("Zamfara", "Zam"),
    ]

    for state_name, abbreviation in states:
        State.objects.get_or_create(
            state_name=state_name,
            defaults={'abbreviation': abbreviation},
        )


def unseed_states(apps, schema_editor):
    State = apps.get_model('betting', 'State')
    State.objects.filter(
        state_name__in=[
            "Abia",
            "Abuja Federal Capital Territory",
            "Adamawa",
            "Akwa Ibom",
            "Anambra",
            "Bauchi",
            "Bayelsa",
            "Benue",
            "Borno",
            "Cross River",
            "Delta",
            "Ebonyi",
            "Edo",
            "Ekiti",
            "Enugu",
            "Gombe",
            "Imo",
            "Jigawa",
            "Kaduna",
            "Kano",
            "Katsina",
            "Kebbi",
            "Kogi",
            "Kwara",
            "Lagos",
            "Nasarawa",
            "Niger",
            "Ogun",
            "Ondo",
            "Osun",
            "Oyo",
            "Plateau",
            "Rivers",
            "Sokoto",
            "Taraba",
            "Yobe",
            "Zamfara",
        ]
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('betting', '0041_state_user_other_name_user_username_user_state'),
    ]

    operations = [
        migrations.RunPython(seed_states, reverse_code=unseed_states),
    ]

