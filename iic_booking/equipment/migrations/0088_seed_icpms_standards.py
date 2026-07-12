# Generated manually on 2026-03-19 (seed ICPMS standard sample database)

from django.db import migrations


def seed_icpms_standards(apps, schema_editor):
    ICPMSStandardSample = apps.get_model("equipment", "ICPMSStandardSample")

    rows = [
        {
            "s_no": "STD - 1",
            "part_no": "8500-6940",
            "name_of_std": "Multi Element calibration STD 2A (Agilent)",
            "list_of_elements": "Ag, Al, As, Ba, Be, Ca, Cd, Co, Cr, Cs, Cu, Fe, Ga, K, Li, Mg, Mn, Na, Ni, Pb, Rb, Se, Sr, Tl, U, V, Zn",
            "concentration": "10 PPM",
            "status": 1,
        },
        {
            "s_no": "STD - 2",
            "part_no": "6610030600",
            "name_of_std": "Calibration Mix 2 (Agilent)",
            "list_of_elements": "Ag, Al, As, Ba, Be, Cd, Co, Cr, Cu, Mn, Ni, Pb, Se, Tl, Th, U, V, Zn",
            "concentration": "100 PPM",
            "status": 1,
        },
        {
            "s_no": "STD - 3",
            "part_no": "8500-6944",
            "name_of_std": "Multi Element Calibration STD 1 (Agilent)",
            "list_of_elements": "Ce, Dy, Er, Eu, Gd, Ho, La, Lu, Nd, Pr, Sc, Sm, Tb, Th, Tm, Y, Yb (Rare Earth Element)",
            "concentration": "10 PPM",
            "status": 1,
        },
        {
            "s_no": "STD - 4",
            "part_no": "8500-6942",
            "name_of_std": "Multi Element Calibration STD 4 (Agilent)",
            "list_of_elements": "B, Ge, Mo, Nb, P, Re, S, Si, Ta, Ti, W, Zr",
            "concentration": "10 PPM",
            "status": 1,
        },
        {
            "s_no": "STD - 5",
            "part_no": "6610030700",
            "name_of_std": "Calibration Mix Majors (Agilent)",
            "list_of_elements": "Ca, Fe, K, Na, Mg",
            "concentration": "500 PPM",
            "status": 1,
        },
        {
            "s_no": "STD - 6",
            "part_no": "HC72516894",
            "name_of_std": "Multi Element STD Solution IX (Merck)",
            "list_of_elements": "As, Be, Cd, Cr, Hg, Ni, Pb, Se, Tl",
            "concentration": "100 PPM",
            "status": 1,
        },
        {
            "s_no": "STD – 7A &7B",
            "part_no": "8500-6948",
            "name_of_std": "Multi Element Calibration STD 3 (Agilent)",
            "list_of_elements": "Au, Hf, Ir, Pd, Pt, Rh, Ru, Sb, Sn, Te",
            "concentration": "10 PPM",
            "status": 1,
        },
        {
            "s_no": "STD - 8",
            "part_no": "5190-8246",
            "name_of_std": "As",
            "list_of_elements": "As",
            "concentration": "1000 PPM",
            "status": 1,
        },
        {
            "s_no": "STD – 9",
            "part_no": "5190-8485",
            "name_of_std": "Hg",
            "list_of_elements": "Hg",
            "concentration": "1000 PPM",
            "status": 1,
        },
        {
            "s_no": "STD – 10 A, 10 B & 10 C",
            "part_no": "8500-6940",
            "name_of_std": "Hg",
            "list_of_elements": "Hg",
            "concentration": "10 PPM",
            "status": 1,
        },
        {
            "s_no": "Std - 11",
            "part_no": "IV-ICPMS-71A",
            "name_of_std": "MULTI ELEMENT STD (Inorganic Ventures)",
            "list_of_elements": "Ag, Al, As, B, Be, Ba, Ca, Cd, Ce, Co, Cr, Cs, Cu, Dy, Er, Eu, Fe, Ga, Gd, Ho, K, La, Lu, Mg, Mn, Na, Nd, Ni, P, Pb, Pr, Rb, S, Se, Sm, Sr, Th, Tl, Tm, U, V, Yb, Zn",
            "concentration": "10 PPM",
            "status": 1,
        },
        {
            "s_no": "STD – 12",
            "part_no": "CCS-1",
            "name_of_std": "Rare Earth Element (Inorganic Ventures)",
            "list_of_elements": "Ce, Dy, Er, Eu, Gd, Ho, La, Lu, Nd, Pr, Sc, Sm, Tb, Th, Tm, Y, Yb (Rare Earth Element)",
            "concentration": "100 PPM",
            "status": 1,
        },
        {
            "s_no": "STD- 13",
            "part_no": "HC90909387",
            "name_of_std": "Multi Std XVI (Merck)",
            "list_of_elements": "Sb, As, Be, Cd, Ca, Cr, Co, Cu, Fe, Pb, Li, Mg, Mn, Mo, Ni, Se, Sr, Tl, Ti, V, Zn",
            "concentration": "100 PPM",
            "status": 1,
        },
    ]

    # Idempotent-ish: upsert by s_no + part_no + name_of_std
    for r in rows:
        ICPMSStandardSample.objects.update_or_create(
            s_no=r["s_no"],
            part_no=r["part_no"],
            name_of_std=r["name_of_std"],
            defaults={
                "list_of_elements": r["list_of_elements"],
                "concentration": r["concentration"],
                "status": r["status"],
            },
        )


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0087_icpms_standard_sample_database"),
    ]

    operations = [
        migrations.RunPython(seed_icpms_standards, migrations.RunPython.noop),
    ]

