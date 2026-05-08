import re
import uuid
from django.db import IntegrityError, transaction


def normalize_name_part(value: str) -> str:
    value = (value or "").strip()
    value = re.sub(r"[^A-Za-z0-9]", "", value)
    if not value:
        return ""
    return value[:1].upper() + value[1:].lower()


def build_root(state_abbreviation: str, name_part: str) -> str:
    abbr = (state_abbreviation or "").strip()
    abbr = re.sub(r"[^A-Za-z0-9]", "", abbr)
    abbr = abbr[:1].upper() + abbr[1:].lower() if abbr else ""
    part = normalize_name_part(name_part)
    return f"{abbr}{part}"


def username_exists(UserModel, username: str) -> bool:
    if not username:
        return False
    return UserModel.objects.filter(username__iexact=username).exists()


def generate_agent_username(UserModel, state_abbreviation: str, first_name: str, last_name: str, other_name: str):
    root1 = build_root(state_abbreviation, first_name)
    root2 = build_root(state_abbreviation, last_name)
    root3 = build_root(state_abbreviation, other_name)

    roots = [root1, root2, root3]
    roots = [r for r in roots if r]
    if not roots:
        raise ValueError("Unable to generate username roots.")

    for candidate in roots:
        if not username_exists(UserModel, candidate):
            return candidate, roots, root1

    base = root1 or roots[0]
    suffix = 1
    while True:
        candidate = f"{base}{suffix}"
        if not username_exists(UserModel, candidate):
            return candidate, roots, base
        suffix += 1


def generate_cashier_usernames(UserModel, preferred_root: str, roots: list[str], base_root: str):
    roots_to_try = []
    if preferred_root:
        roots_to_try.append(preferred_root)
    for r in roots:
        if r and r not in roots_to_try:
            roots_to_try.append(r)

    for root in roots_to_try:
        c1 = f"{root}C1"
        c2 = f"{root}C2"
        if not username_exists(UserModel, c1) and not username_exists(UserModel, c2):
            return c1, c2, root

    base = base_root or (roots[0] if roots else preferred_root)
    counter = 1
    while True:
        root = f"{base}{counter}"
        c1 = f"{root}C1"
        c2 = f"{root}C2"
        if not username_exists(UserModel, c1) and not username_exists(UserModel, c2):
            return c1, c2, root
        counter += 1


def generate_internal_email(prefix: str) -> str:
    safe_prefix = re.sub(r"[^a-z0-9]+", "", (prefix or "").lower())[:30] or "user"
    return f"{safe_prefix}-{uuid.uuid4().hex}@internal.invalid"


def generate_cashier_email(agent_email: str, cashier_code: str) -> str:
    email = (agent_email or "").strip()
    local, sep, domain = email.partition("@")
    if not sep or not local or not domain:
        return generate_internal_email(f"{cashier_code}{email}")

    safe_code = re.sub(r"[^A-Za-z0-9]+", "", (cashier_code or "").strip()) or "C"
    return f"{safe_code}{local}@{domain}"


@transaction.atomic
def create_agent_and_cashiers(
    UserModel,
    *,
    email: str,
    password: str,
    first_name: str,
    last_name: str,
    other_name: str,
    state,
    master_agent=None,
    super_agent=None,
    phone_number: str | None = None,
    shop_address: str | None = None,
):
    agent_username, roots, base_root = generate_agent_username(
        UserModel,
        state.abbreviation,
        first_name,
        last_name,
        other_name,
    )

    agent = UserModel(
        email=email,
        username=agent_username,
        first_name=first_name,
        last_name=last_name,
        other_name=other_name,
        state=state,
        phone_number=phone_number,
        shop_address=shop_address,
        user_type='agent',
        is_active=True,
        is_staff=True,
        is_superuser=False,
        master_agent=master_agent,
        super_agent=super_agent,
        agent=None,
    )
    agent.set_password(password)

    try:
        agent.save()
    except IntegrityError:
        agent_username, roots, base_root = generate_agent_username(
            UserModel,
            state.abbreviation,
            first_name,
            last_name,
            other_name,
        )
        agent.username = agent_username
        agent.save()

    cashier1_username, cashier2_username, cashier_root = generate_cashier_usernames(
        UserModel,
        preferred_root=agent.username,
        roots=roots,
        base_root=base_root,
    )

    cashier1 = UserModel(
        email=generate_cashier_email(agent.email, "C1"),
        username=cashier1_username,
        first_name=first_name,
        last_name=last_name,
        other_name=other_name,
        state=state,
        user_type='cashier',
        agent=agent,
        master_agent=agent.master_agent,
        super_agent=agent.super_agent,
        is_active=True,
        is_staff=True,
        is_superuser=False,
    )
    cashier1.set_password(password)

    cashier2 = UserModel(
        email=generate_cashier_email(agent.email, "C2"),
        username=cashier2_username,
        first_name=first_name,
        last_name=last_name,
        other_name=other_name,
        state=state,
        user_type='cashier',
        agent=agent,
        master_agent=agent.master_agent,
        super_agent=agent.super_agent,
        is_active=True,
        is_staff=True,
        is_superuser=False,
    )
    cashier2.set_password(password)

    cashier1.save()
    cashier2.save()

    return agent, [cashier1, cashier2], cashier_root
