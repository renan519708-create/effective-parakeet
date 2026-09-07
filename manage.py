"""CLI management commands.

    python manage.py create-owner --email you@example.com --password ...
    python manage.py create-invite --by you@example.com
    python manage.py runserver

create-owner exists because signup is invite-only (see auth.py) --
there's no way to create the very first account through the web UI, so
this bootstraps it directly against the database.
"""

import argparse
import secrets

from werkzeug.security import generate_password_hash

from app import create_app
from app.extensions import db
from app.models import FollowerSettings, InviteCode, User


def create_owner(email, password):
    app = create_app(start_engine=False)
    with app.app_context():
        if User.query.filter_by(email=email).first():
            print(f"Ja existe uma conta com o email {email}.")
            return
        user = User(email=email, password_hash=generate_password_hash(password), role="owner")
        db.session.add(user)
        db.session.flush()
        db.session.add(FollowerSettings(user_id=user.id))
        db.session.commit()
        print(f"Owner criado: {email}")


def create_invite(by_email):
    app = create_app(start_engine=False)
    with app.app_context():
        owner = User.query.filter_by(email=by_email).first()
        if not owner:
            print(f"Conta {by_email} nao encontrada.")
            return
        code = secrets.token_urlsafe(9)
        db.session.add(InviteCode(code=code, created_by_id=owner.id))
        db.session.commit()
        print(f"Codigo de convite: {code}")


def runserver():
    app = create_app()
    app.run(debug=True, port=5000)


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    p_owner = sub.add_parser("create-owner")
    p_owner.add_argument("--email", required=True)
    p_owner.add_argument("--password", required=True)

    p_invite = sub.add_parser("create-invite")
    p_invite.add_argument("--by", required=True)

    sub.add_parser("runserver")

    args = parser.parse_args()
    if args.command == "create-owner":
        create_owner(args.email, args.password)
    elif args.command == "create-invite":
        create_invite(args.by)
    elif args.command == "runserver":
        runserver()


if __name__ == "__main__":
    main()
