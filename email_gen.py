import csv
import html
import sys
import urllib.parse

SENDER = "apply@thetriangle.org"
SUBJECT = "Welcome to The Triangle!"
FORM_TEXT = "The Triangle's Staff Development Day:"
FORM_URL = "https://docs.google.com/forms/d/e/1FAIpQLSe5Rui2fOt2YIdHuxWC8yAPJS0Pw50QcFraNAG-OMlPvjNZ_g/viewform"

# applications.csv holds two tables side by side:
#   applicants: Name, Section 1, Section 2, Contact, Emailed?
#   editors:    two editor emails, Section (columns 6, 7, 8)
NAME, SECTION_1, SECTION_2, CONTACT = 0, 1, 2, 3
EDITOR_1, EDITOR_2, EDITOR_SECTION = 6, 7, 8

# Sections led by a manager rather than editors.
MANAGED_SECTIONS = {"Business"}


def is_blank(value):
    return not value or value.strip().upper() == "N/A"


def name_from_email(email):
    # erik.heyman-meltzer@thetriangle.org -> Erik Heyman-Meltzer
    local = email.split("@")[0]
    return " ".join(part.title() for part in local.split("."))


def join_and(items):
    if len(items) <= 2:
        return " and ".join(items)
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def read_csv(path):
    applicants = []
    editors = {}

    with open(path, mode="r", newline="") as file:
        reader = csv.reader(file)
        next(reader)

        for row in reader:
            row += [""] * (EDITOR_SECTION + 1 - len(row))

            if not is_blank(row[EDITOR_SECTION]):
                emails = [row[i].strip() for i in (EDITOR_1, EDITOR_2) if not is_blank(row[i])]
                editors[row[EDITOR_SECTION].strip()] = emails

            if not is_blank(row[NAME]):
                sections = [row[i].strip() for i in (SECTION_1, SECTION_2) if not is_blank(row[i])]
                applicants.append((row[NAME].strip(), sections, row[CONTACT].strip()))

    return applicants, editors


def contact_sentence(section, emails):
    role = "Manager" if section in MANAGED_SECTIONS else "editor"
    names = join_and([name_from_email(e) for e in emails])
    addresses = join_and(emails)
    if len(emails) == 1:
        return f"For {section}, your {role} is {names}, who you can reach at {addresses}."
    return f"Your {role}s for {section} are {names}. You can reach them at {addresses}."


def write_email(name, sections, editors):
    first_name = name.split()[0]
    team_names = join_and(sections) + (" Teams" if len(sections) > 1 else " Team")

    if len(sections) > 1:
        intro = f"As you are now a member of both the {' AND '.join(sections)} teams, you will have multiple section directors."
    else:
        intro = f"As you are now a member of the {sections[0]} team,"
    contacts = " ".join(contact_sentence(s, editors[s]) for s in sections if editors[s])
    if len(sections) == 1:
        # Lower-case the first word so it reads on from the intro clause.
        contacts = contacts[0].lower() + contacts[1:]

    return f"""{first_name},

On behalf of everyone at The Triangle, congratulations! We'd like to formally invite you to join us as a member of our {team_names}! The Triangle is an extremely unique organization to Drexel and we are so excited to welcome you to our community.

If you choose to accept your position at The Triangle, please reply to this email as soon as possible. If you choose to accept your position at The Triangle, please join us for our General Body Meeting on Wednesday, October 21st at 6:30 p.m. in our office in the basement of the Creese Student Center, Suite 050.

Please join us for our MANDATORY development day on Sunday, October 25th from 10:00 a.m. to 3 p.m. We will be hosting this event in Lebow for all new members to be introduced to The Triangle. Throughout the day, attendees will meet current Triangle staff, participate in activities, and hear from guest speakers with professional experience in media, journalism, and business, who will share their insights, experiences, and advice. We will provide breakfast and lunch for all attendees. Please confirm your attendance by completing the form linked below so we can plan accordingly.

{FORM_TEXT}
{FORM_URL}

In addition, if you have any questions, comments or concerns that need to be addressed, please reach out either to this email address, or by reaching out to your section editor directly! {intro} {contacts}

Once again, we are so excited to have you join us at The Triangle, we look forward to getting to meet you this week!

Sincerely,
"""


def compose_url(to, body):
    # Opens a prefilled Gmail compose window in the SENDER account.
    params = {"view": "cm", "fs": "1", "authuser": SENDER, "to": to, "su": SUBJECT, "body": body}
    return "https://mail.google.com/mail/?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)


def compose_page(rows):
    items = "".join(
        f'<li><a href="{html.escape(url)}" target="_blank">{html.escape(name)}</a></li>\n'
        for name, _, _, url in rows
    )
    return f"""<!doctype html>
<meta charset="utf-8">
<title>Acceptance emails</title>
<ul>
{items}</ul>
"""


def main(args):
    if len(args) not in (2, 3):
        sys.exit(f"usage: {args[0]} <csv_file> [output_file]")
    out_path = args[2] if len(args) == 3 else "acceptance_emails.html"

    applicants, editors = read_csv(args[1])

    unknown = {s for _, sections, _ in applicants for s in sections if s not in editors}
    if unknown:
        sys.exit(f"no editors listed for section(s): {', '.join(sorted(unknown))}")

    rows = [
        (name, sections, contact, compose_url(contact, write_email(name, sections, editors)))
        for name, sections, contact in applicants
    ]
    with open(out_path, "w") as file:
        file.write(compose_page(rows))

    print(f"wrote {len(rows)} compose links to {out_path}")


if __name__ == "__main__":
    main(sys.argv)
