"""Customer directory, purchase insights, merge workflow, and quick-create API."""

from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from homestead.db.customers import (
    PREFERRED_CONTACTS,
    customer_insights_summary,
    delete_customer,
    get_customer,
    list_customer_birds,
    merge_customers,
    save_customer,
)
from homestead.web import _fmt_lb_oz, _render, current_user

router = APIRouter()


@router.get("/customers")
def customers_page(
    request: Request,
    q: str = "",
    selected: str = "",
    msg: str = "",
    user: dict = Depends(current_user),
):
    insights = customer_insights_summary(user["id"], search=q)
    customers = insights["customers"]
    sel_id = int(selected) if selected.strip().isdigit() else None
    sel_customer = next((c for c in customers if int(c["id"]) == sel_id), None) if sel_id else None
    if sel_id and not sel_customer:
        sel_customer = get_customer(user["id"], sel_id)
    sel_birds = list_customer_birds(user["id"], sel_id) if sel_customer else []
    for bird in sel_birds:
        bird["lb_oz"] = _fmt_lb_oz(bird["dressed_lbs"])
        calc_p = round(float(bird["dressed_lbs"] or 0) * float(bird["sale_price_per_lb"] or 0), 2)
        act_p = (
            round(float(bird["actual_sale_price"]), 2)
            if bird.get("actual_sale_price") is not None
            else calc_p
        )
        bird["calculated_price"] = calc_p
        bird["actual_price"] = act_p
        bird["tip_diff"] = round(act_p - calc_p, 2)
    return _render(
        request, "customers.html", user, "customers",
        customers=customers,
        insights=insights,
        q=q,
        sel_customer=sel_customer,
        sel_birds=sel_birds,
        preferred_contacts=PREFERRED_CONTACTS,
        msg=msg,
    )


@router.post("/customers/save")
def customers_save_submit(
    customer_id: str = Form(""),
    name: str = Form(...),
    phone: str = Form(""),
    email: str = Form(""),
    preferred_contact: str = Form("sms"),
    notes: str = Form(""),
    user: dict = Depends(current_user),
):
    cid = int(customer_id) if customer_id.strip().isdigit() else None
    try:
        saved_id = save_customer(
            user["id"], name=name, phone=phone, email=email,
            preferred_contact=preferred_contact, notes=notes, customer_id=cid,
        )
        return RedirectResponse(
            f"/customers?selected={saved_id}&msg=" + quote_plus(f"Saved {name.strip()}."),
            status_code=303,
        )
    except ValueError as exc:
        return RedirectResponse("/customers?msg=" + quote_plus(str(exc)), status_code=303)


@router.post("/customers/{customer_id}/delete")
def customers_delete_submit(customer_id: int, user: dict = Depends(current_user)):
    delete_customer(user["id"], customer_id)
    return RedirectResponse("/customers?msg=" + quote_plus("Customer deleted."), status_code=303)


@router.post("/customers/{customer_id}/merge")
def customers_merge_submit(
    customer_id: int,
    target_customer_id: int = Form(...),
    user: dict = Depends(current_user),
):
    try:
        merge_customers(user["id"], customer_id, target_customer_id)
        return RedirectResponse(
            f"/customers?selected={target_customer_id}&msg=" + quote_plus("Merged duplicate customer records."),
            status_code=303,
        )
    except ValueError as exc:
        return RedirectResponse(
            f"/customers?selected={customer_id}&msg=" + quote_plus(str(exc)),
            status_code=303,
        )


@router.post("/api/customers")
async def api_customer_create(request: Request, user: dict = Depends(current_user)):
    payload = await request.json()
    try:
        cid = save_customer(
            user["id"],
            name=str(payload.get("name") or ""),
            phone=str(payload.get("phone") or ""),
            email=str(payload.get("email") or ""),
            preferred_contact=str(payload.get("preferred_contact") or "sms"),
            notes=str(payload.get("notes") or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return get_customer(user["id"], cid)


# ---------------------------------------------------------------------------
