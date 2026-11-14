"""Authentication gate, persistent session management, and User / Admin management modal."""

import json
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
from homestead.telemetry import (
    clear_telemetry_events,
    get_telemetry_summary,
    list_telemetry_events,
    log_telemetry_event,
    record_ui_action,
)

SESSION_COOKIE_NAME = "homestead_session"


def _get_cookie_session_token() -> str | None:
    """Safely read the session token from the browser request cookies."""
    try:
        cookies = st.context.cookies
        if cookies and SESSION_COOKIE_NAME in cookies:
            val = str(cookies[SESSION_COOKIE_NAME]).strip()
            return val or None
    except Exception:
        pass
    return None


def _strip_legacy_session_query_param() -> None:
    """Remove any 'session' query parameter from the URL so tokens never appear or authenticate via URL."""
    try:
        if "session" in st.query_params:
            del st.query_params["session"]
    except Exception:
        pass


def _render_cookie_iframe(html_snippet: str) -> None:
    """Render an inline HTML/JS snippet via st.iframe (with fallback for Streamlit < 1.56)."""
    iframe_fn = getattr(st, "iframe", None)
    if callable(iframe_fn):
        iframe_fn(html_snippet, height=1)
    else:
        import streamlit.components.v1 as components

        components.html(html_snippet, height=0)


def _sync_session_cookie_component() -> None:
    """Synchronize browser cookie state when a login or logout event occurs."""
    token_to_set = st.session_state.pop("_pending_cookie_set", None)
    should_clear = st.session_state.pop("_pending_cookie_clear", False)
    if token_to_set:
        safe_token_js = json.dumps(str(token_to_set))
        _render_cookie_iframe(
            f"""<script>
            (function() {{
                var token = {safe_token_js};
                var secure = (window.parent.location.protocol === "https:") ? "; Secure" : "";
                var cookieStr = "{SESSION_COOKIE_NAME}=" + encodeURIComponent(token) + "; Path=/; Max-Age=2592000; SameSite=Lax" + secure;
                try {{ window.parent.document.cookie = cookieStr; }} catch (e) {{ document.cookie = cookieStr; }}
            }})();
            </script>"""
        )
    elif should_clear:
        _render_cookie_iframe(
            f"""<script>
            (function() {{
                var secure = (window.parent.location.protocol === "https:") ? "; Secure" : "";
                var cookieStr = "{SESSION_COOKIE_NAME}=; Path=/; Max-Age=0; SameSite=Lax" + secure;
                try {{ window.parent.document.cookie = cookieStr; }} catch (e) {{ document.cookie = cookieStr; }}
            }})();
            </script>"""
        )


def login_user_session(user: dict, action_label: str = "Sign In") -> None:
    """Establish an authenticated session in session_state and browser cookie (never in URL query_params)."""
    token = create_user_session(int(user["id"]))
    st.session_state.current_user_id = int(user["id"])
    st.session_state.session_token = token
    st.session_state.pop("_logged_out_this_tab", None)
    st.session_state._pending_cookie_set = token
    _strip_legacy_session_query_param()
    record_ui_action(
        action_label,
        event_type="auth",
        details={"username": user.get("username", "")},
    )


def logout_user_session() -> None:
    """Terminate the current user session and expire the browser session cookie."""
    uid = st.session_state.get("current_user_id")
    log_telemetry_event(
        event_type="auth",
        action="Sign Out",
        user_id=int(uid) if uid is not None else None,
    )
    token = st.session_state.get("session_token") or _get_cookie_session_token()
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
        "sidebar_species_filter",
        "_known_species_present",
        "sidebar_status_filter",
    ):
        st.session_state.pop(k, None)
    st.session_state._logged_out_this_tab = True
    st.session_state._pending_cookie_clear = True
    _strip_legacy_session_query_param()


def ensure_authenticated_user() -> dict | None:
    """Return the authenticated user dict if logged in or restorable from browser cookie."""
    _strip_legacy_session_query_param()
    _sync_session_cookie_component()

    uid = st.session_state.get("current_user_id")
    if uid is not None:
        user = get_user_by_id(int(uid))
        if user:
            return user
        st.session_state.pop("current_user_id", None)

    if st.session_state.get("_logged_out_this_tab"):
        return None

    token = _get_cookie_session_token()
    if token:
        user = get_user_by_session_token(str(token))
        if user:
            st.session_state.current_user_id = int(user["id"])
            st.session_state.session_token = str(token)
            return user
        st.session_state._pending_cookie_clear = True
        _sync_session_cookie_component()

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
                    width="stretch",
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
                    width="stretch",
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
                        width="stretch",
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
        tab_users, tab_telemetry, tab_pw = st.tabs(
            [
                "👥 Manage Users (Admin)",
                "📊 Telemetry & Timing",
                "🔑 Change My Password",
            ]
        )
    else:
        tab_users = None
        tab_telemetry = None
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
                    log_telemetry_event(
                        event_type="auth",
                        action="Update Own Password",
                        user_id=int(current_user["id"]),
                        username=str(current_user.get("username", "")),
                    )
                    st.success("Password updated!")

    if tab_telemetry is not None:
        with tab_telemetry:
            summary = get_telemetry_summary()
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("🖱️ Tracked Events", f"{summary['total_events']:,}")
            m2.metric("⚡ Avg Latency", f"{summary['avg_ms']:,.1f} ms")
            m3.metric(
                "📈 P95 / Peak",
                f"{summary['p95_ms']:,.1f} ms",
                delta=f"Max {summary['max_ms']:,.1f} ms",
                delta_color="off",
            )
            m4.metric("👥 Active Users", f"{summary['unique_users']:,}")

            if summary["by_workspace"] or summary["by_user"]:
                tc1, tc2 = st.columns(2)
                with tc1:
                    st.markdown("##### 🗂️ Workspace Usage & Timing")
                    st.dataframe(
                        summary["by_workspace"],
                        width="stretch",
                        hide_index=True,
                    )
                with tc2:
                    st.markdown("##### 👤 Activity by User")
                    st.dataframe(
                        summary["by_user"],
                        width="stretch",
                        hide_index=True,
                    )

            if summary["by_action"]:
                st.markdown("##### 🎯 Click & Action Breakdown")
                st.dataframe(
                    summary["by_action"],
                    width="stretch",
                    hide_index=True,
                )

            st.markdown("##### 🕒 Recent Click & Timing Stream")
            f_c1, f_c2, f_c3 = st.columns([1.5, 1.5, 1])
            user_opts = ["All Users"] + sorted(
                {u["username"] for u in list_users()}
            )
            with f_c1:
                sel_u_filter = st.selectbox(
                    "Filter by User",
                    options=user_opts,
                    key="telemetry_user_filter",
                )
            with f_c2:
                sel_type_filter = st.selectbox(
                    "Filter by Event Type",
                    options=[
                        "All Types",
                        "navigation",
                        "click",
                        "modal",
                        "mutation",
                        "auth",
                        "export",
                    ],
                    key="telemetry_type_filter",
                )
            with f_c3:
                if st.button(
                    "🗑️ Clear Logs",
                    key="clear_telemetry_btn",
                    width="stretch",
                ):
                    clear_telemetry_events()
                    st.rerun()

            recent_rows = list_telemetry_events(
                limit=150,
                username_filter=sel_u_filter,
                event_type_filter=sel_type_filter,
            )
            if recent_rows:
                display_stream = [
                    {
                        "Time (UTC)": r["created_at"],
                        "User": f"@{r['username']}" if r["username"] else "—",
                        "Type": r["event_type"],
                        "Action": r["action"],
                        "Workspace": r["workspace"],
                        "Timing (ms)": r["duration_ms"],
                        "Details": r["details"],
                    }
                    for r in recent_rows
                ]
                st.dataframe(
                    display_stream,
                    width="stretch",
                    hide_index=True,
                )
            else:
                st.caption("No telemetry events recorded yet.")

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
                                    width="stretch",
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
                                width="stretch",
                            ):
                                delete_user(uid)
                                st.rerun()

    st.divider()
    st.button(
        "Close",
        key="close_user_modal_btn",
        width="stretch",
        on_click=_dismiss_user_modal,
    )
