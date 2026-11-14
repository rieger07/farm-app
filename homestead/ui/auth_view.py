"""Authentication gate, persistent session management, and User / Admin management modal."""

import streamlit as st

from homestead.database import (
    authenticate_user,
    count_users,
    create_user,
    create_user_session,
    delete_user,
    delete_user_session,
    get_user_by_id,
    get_user_by_session_token,
    is_public_registration_enabled,
    list_users,
    set_public_registration_enabled,
    update_user_password,
)


def login_user_session(user: dict) -> None:
    """Establish an authenticated session in session_state and URL query_params."""
    token = create_user_session(int(user["id"]))
    st.session_state.current_user_id = int(user["id"])
    st.session_state.session_token = token
    try:
        st.query_params["session"] = token
    except Exception:
        pass


def logout_user_session() -> None:
    """Terminate the current user session and clear query_params."""
    token = st.session_state.get("session_token")
    if not token:
        try:
            token = st.query_params.get("session")
        except Exception:
            token = None
    if token:
        delete_user_session(str(token))
    for k in (
        "current_user_id",
        "session_token",
        "selected_batch_id",
        "editing_batch_id",
        "pending_edit_batch",
        "show_batch_modal",
        "show_species_modal",
        "show_user_modal",
    ):
        st.session_state.pop(k, None)
    try:
        if "session" in st.query_params:
            del st.query_params["session"]
    except Exception:
        pass


def ensure_authenticated_user() -> dict | None:
    """Return the authenticated user dict if logged in or restorable from session token."""
    uid = st.session_state.get("current_user_id")
    if uid is not None:
        user = get_user_by_id(int(uid))
        if user:
            return user
        st.session_state.pop("current_user_id", None)

    try:
        token = st.query_params.get("session")
    except Exception:
        token = None

    if token:
        user = get_user_by_session_token(str(token))
        if user:
            st.session_state.current_user_id = int(user["id"])
            st.session_state.session_token = str(token)
            return user
        try:
            del st.query_params["session"]
        except Exception:
            pass

    return None


def render_auth_gate() -> None:
    """Render the First-Run Admin onboarding or Sign-In / Self-Registration view."""
    _, center_col, _ = st.columns([1, 1.6, 1])
    with center_col:
        st.markdown("## 🚜 Homestead Planner")
        st.caption("Poultry, Pigs & Custom Livestock Operations, Feed & Freezer")

        total_users = count_users()
        if total_users == 0:
            with st.form("setup_admin_form", border=True):
                st.markdown("### 🛡️ Create Administrator Account")
                st.info(
                    "👋 **Welcome!** Create the first administrator account to secure your Homestead Planner. "
                    "Any existing batches, logs, and settings in your database will automatically be claimed by this account."
                )
                admin_username = st.text_input(
                    "Admin Username",
                    key="setup_admin_username",
                    placeholder="e.g. srieger",
                )
                admin_display = st.text_input(
                    "Display / Farm Name (optional)",
                    key="setup_admin_display",
                    placeholder="e.g. Rieger Homestead",
                )
                admin_pw = st.text_input(
                    "Password",
                    type="password",
                    key="setup_admin_password",
                    placeholder="At least 4 characters",
                )
                admin_pw_confirm = st.text_input(
                    "Confirm Password",
                    type="password",
                    key="setup_admin_password_confirm",
                )
                submitted_setup = st.form_submit_button(
                    "🚀 Create Admin Account & Sign In",
                    key="setup_admin_submit_btn",
                    type="primary",
                    use_container_width=True,
                )
                if submitted_setup:
                    if not admin_username.strip():
                        st.error("Please enter a username.")
                    elif len(admin_pw) < 4:
                        st.error("Password must be at least 4 characters.")
                    elif admin_pw != admin_pw_confirm:
                        st.error("Passwords do not match.")
                    else:
                        try:
                            new_user = create_user(
                                username=admin_username,
                                password=admin_pw,
                                display_name=admin_display,
                                is_admin=True,
                                claim_unowned_data=True,
                            )
                            login_user_session(new_user)
                            st.rerun()
                        except ValueError as exc:
                            st.error(str(exc))
            return

        public_reg = is_public_registration_enabled()
        if public_reg:
            tab_login, tab_register = st.tabs(
                ["🔐 Sign In", "📝 Create Account"]
            )
        else:
            tab_login = st.container()
            tab_register = None

        with tab_login:
            with st.form("login_form", border=True):
                st.markdown("### 🔐 Sign In")
                login_username = st.text_input(
                    "Username",
                    key="login_username_input",
                )
                login_pw = st.text_input(
                    "Password",
                    type="password",
                    key="login_password_input",
                )
                submitted_login = st.form_submit_button(
                    "🔐 Sign In",
                    key="login_submit_btn",
                    type="primary",
                    use_container_width=True,
                )
                if submitted_login:
                    user = authenticate_user(login_username, login_pw)
                    if user:
                        login_user_session(user)
                        st.rerun()
                    else:
                        st.error("Invalid username or password.")
                if not public_reg:
                    st.caption(
                        "🔒 Public self-registration is disabled. Contact the farm administrator for an account."
                    )

        if tab_register is not None:
            with tab_register:
                with st.form("register_form", border=True):
                    st.markdown("### 📝 Create New Account")
                    reg_username = st.text_input(
                        "Username",
                        key="reg_username_input",
                    )
                    reg_display = st.text_input(
                        "Display / Farm Name (optional)",
                        key="reg_display_input",
                    )
                    reg_pw = st.text_input(
                        "Password",
                        type="password",
                        key="reg_password_input",
                        placeholder="At least 4 characters",
                    )
                    reg_pw_confirm = st.text_input(
                        "Confirm Password",
                        type="password",
                        key="reg_password_confirm_input",
                    )
                    submitted_reg = st.form_submit_button(
                        "📝 Create Account & Sign In",
                        key="reg_submit_btn",
                        type="primary",
                        use_container_width=True,
                    )
                    if submitted_reg:
                        if not reg_username.strip():
                            st.error("Please enter a username.")
                        elif len(reg_pw) < 4:
                            st.error("Password must be at least 4 characters.")
                        elif reg_pw != reg_pw_confirm:
                            st.error("Passwords do not match.")
                        else:
                            try:
                                created = create_user(
                                    username=reg_username,
                                    password=reg_pw,
                                    display_name=reg_display,
                                    is_admin=False,
                                    claim_unowned_data=False,
                                )
                                login_user_session(created)
                                st.rerun()
                            except ValueError as exc:
                                st.error(str(exc))


def _dismiss_user_modal() -> None:
    st.session_state.show_user_modal = False


@st.dialog(
    "👥 Account & User Management",
    width="large",
    on_dismiss=_dismiss_user_modal,
)
def render_user_admin_modal(current_user: dict) -> None:
    if not st.session_state.get("show_user_modal", False):
        st.rerun(scope="app")

    st.markdown(
        f"**Signed in as:** `{current_user['display_name']}` (`@{current_user['username']}`) "
        f"{'· 🛡️ **Admin**' if current_user.get('is_admin') else ''}"
    )

    if current_user.get("is_admin"):
        tab_users, tab_pw = st.tabs(
            ["👥 Manage Users (Admin)", "🔑 Change My Password"]
        )
    else:
        tab_users = None
        tab_pw = st.container()

    with tab_pw:
        with st.form("self_update_pw_form", border=True):
            st.markdown("#### 🔑 Update Your Password")
            new_pw = st.text_input(
                "New Password",
                type="password",
                key="self_new_pw_input",
                placeholder="At least 4 characters",
            )
            confirm_pw = st.text_input(
                "Confirm New Password",
                type="password",
                key="self_confirm_pw_input",
            )
            if st.form_submit_button(
                "💾 Update My Password",
                key="self_update_pw_btn",
                type="primary",
            ):
                if len(new_pw) < 4:
                    st.error("Password must be at least 4 characters.")
                elif new_pw != confirm_pw:
                    st.error("Passwords do not match.")
                else:
                    update_user_password(int(current_user["id"]), new_pw)
                    st.success("Password updated!")

    if tab_users is not None:
        with tab_users:
            pub_enabled = is_public_registration_enabled()
            new_pub_enabled = st.toggle(
                "Allow public self-registration on login screen",
                value=pub_enabled,
                key="admin_toggle_public_reg",
                help="When enabled, visitors can create their own isolated farm account from the login page.",
            )
            if bool(new_pub_enabled) != pub_enabled:
                set_public_registration_enabled(bool(new_pub_enabled))
                st.rerun()

            st.divider()
            with st.form("adm_create_user_form", border=True):
                st.markdown("#### ➕ Create New User Account")
                u_c1, u_c2, u_c3 = st.columns(3)
                with u_c1:
                    adm_new_username = st.text_input(
                        "Username",
                        key="adm_create_username",
                        placeholder="e.g. neighbor_farm",
                    )
                with u_c2:
                    adm_new_display = st.text_input(
                        "Display Name",
                        key="adm_create_display",
                        placeholder="e.g. Neighbor Farm",
                    )
                with u_c3:
                    adm_new_pw = st.text_input(
                        "Initial Password",
                        type="password",
                        key="adm_create_password",
                        placeholder="At least 4 chars",
                    )
                adm_new_is_admin = st.checkbox(
                    "Grant Administrator privileges",
                    key="adm_create_is_admin",
                    value=False,
                )
                if st.form_submit_button(
                    "➕ Create User Account",
                    key="adm_create_user_btn",
                    type="primary",
                ):
                    try:
                        created_u = create_user(
                            username=adm_new_username,
                            password=adm_new_pw,
                            display_name=adm_new_display,
                            is_admin=adm_new_is_admin,
                            claim_unowned_data=False,
                        )
                        st.success(
                            f"Created account @{created_u['username']} ({created_u['display_name']})!"
                        )
                        st.rerun()
                    except ValueError as exc:
                        st.error(str(exc))

            st.divider()
            st.markdown("#### 📋 Existing Accounts")
            all_users = list_users()
            for u in all_users:
                uid = int(u["id"])
                is_self = uid == int(current_user["id"])
                role_badge = "🛡️ Admin" if u["is_admin"] else "👤 User"
                with st.container(border=True):
                    r_c1, r_c2, r_c3 = st.columns([2.2, 2.2, 0.8])
                    with r_c1:
                        st.markdown(
                            f"**{u['display_name']}** (`@{u['username']}`) · {role_badge}"
                            + (" · *(You)*" if is_self else "")
                        )
                        st.caption(f"Created: {u['created_at']}")
                    with r_c2:
                        with st.form(f"adm_reset_pw_form_{uid}", border=False):
                            pw_c1, pw_c2 = st.columns([1.6, 1])
                            with pw_c1:
                                reset_pw_val = st.text_input(
                                    "Reset Password",
                                    type="password",
                                    key=f"adm_reset_pw_val_{uid}",
                                    placeholder="New password...",
                                    label_visibility="collapsed",
                                )
                            with pw_c2:
                                if st.form_submit_button(
                                    "🔑 Reset PW",
                                    key=f"adm_reset_pw_btn_{uid}",
                                    use_container_width=True,
                                ):
                                    try:
                                        update_user_password(uid, reset_pw_val)
                                        st.success(
                                            f"Reset password for @{u['username']}."
                                        )
                                    except ValueError as exc:
                                        st.error(str(exc))
                    with r_c3:
                        if not is_self:
                            if st.button(
                                "🗑️ Delete",
                                key=f"adm_del_user_btn_{uid}",
                                use_container_width=True,
                            ):
                                delete_user(uid)
                                st.rerun()

    st.divider()
    st.button(
        "Close",
        key="close_user_modal_btn",
        use_container_width=True,
        on_click=_dismiss_user_modal,
    )
