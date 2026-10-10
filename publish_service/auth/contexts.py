from contextvars import ContextVar


current_user_id = ContextVar(
    "current_user_id",
    default=None
)


current_user_uid = ContextVar(
    "current_user_uid",
    default=None
)


current_tenant_id = ContextVar(
    "current_tenant_id",
    default=None
)



def set_user_context(
    user_id,
    user_uid,
    tenant_id=None
):

    current_user_id.set(
        user_id
    )

    current_user_uid.set(
        user_uid
    )

    current_tenant_id.set(
        tenant_id
    )



def get_user_id():

    return current_user_id.get()



def get_user_uid():

    return current_user_uid.get()



def get_tenant_id():

    return current_tenant_id.get()