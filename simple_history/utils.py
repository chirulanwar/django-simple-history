from django.db import transaction
from django.db.models import Case, ForeignKey, ManyToManyField, Q, When
from django.forms.models import model_to_dict

from simple_history.exceptions import AlternativeManagerError, NotHistoricalModelError


def update_change_reason(instance, reason):
    attrs = {}
    model = type(instance)
    manager = instance if instance.pk is not None else model
    history = get_history_manager_for_model(manager)
    history_fields = [field.attname for field in history.model._meta.fields]
    for field in instance._meta.fields:
        if field.attname not in history_fields:
            continue
        value = getattr(instance, field.attname)
        if field.primary_key is True:
            if value is not None:
                attrs[field.attname] = value
        else:
            attrs[field.attname] = value

    record = history.filter(**attrs).order_by("-history_date").first()
    record.history_change_reason = reason
    record.save()


def get_history_manager_for_model(model):
    """Return the history manager for a given app model."""
    try:
        manager_name = model._meta.simple_history_manager_attribute
    except AttributeError:
        raise NotHistoricalModelError(f"Cannot find a historical model for {model}.")
    return getattr(model, manager_name)


def get_history_manager_from_history(history_instance):
    """
    Return the history manager, based on an existing history instance.
    """
    key_name = get_app_model_primary_key_name(history_instance.instance_type)
    return get_history_manager_for_model(history_instance.instance_type).filter(
        **{key_name: getattr(history_instance, key_name)}
    )


def get_history_model_for_model(model):
    """Return the history model for a given app model."""
    return get_history_manager_for_model(model).model


def get_app_model_primary_key_name(model):
    """Return the primary key name for a given app model."""
    if isinstance(model._meta.pk, ForeignKey):
        return model._meta.pk.name + "_id"
    return model._meta.pk.name


def bulk_create_with_history(
    objs,
    model,
    batch_size=None,
    ignore_conflicts=False,
    default_user=None,
    default_change_reason=None,
    default_date=None,
    update_conflicts=False,
):
    """
    Bulk create the objects specified by objs while also bulk creating
    their history (all in one transaction).
    Because of not providing primary key attribute after bulk_create on any DB except
    Postgres (https://docs.djangoproject.com/en/2.2/ref/models/querysets/#bulk-create)
    Divide this process on two transactions for other DB's
    :param objs: List of objs (not yet saved to the db) of type model
    :param model: Model class that should be created
    :param batch_size: Number of objects that should be created in each batch
    :param default_user: Optional user to specify as the history_user in each historical
        record
    :param default_change_reason: Optional change reason to specify as the change_reason
        in each historical record
    :param default_date: Optional date to specify as the history_date in each historical
        record
    :return: List of objs with IDs
    """
    # Exclude ManyToManyFields because they end up as invalid kwargs to
    # model.objects.filter(...) below.
    exclude_fields = [
        field.name
        for field in model._meta.get_fields()
        if isinstance(field, ManyToManyField)
    ]
    history_manager = get_history_manager_for_model(model)
    model_manager = model._default_manager

    second_transaction_required = True
    with transaction.atomic(savepoint=False):
        objs_with_id = model_manager.bulk_create(
            objs, batch_size=batch_size, ignore_conflicts=ignore_conflicts, update_conflicts=update_conflicts
        )
        if objs_with_id and objs_with_id[0].pk and (not ignore_conflicts or update_conflicts):
            second_transaction_required = False
            history_manager.bulk_history_create(
                objs_with_id,
                batch_size=batch_size,
                default_user=default_user,
                default_change_reason=default_change_reason,
                default_date=default_date,
            )
    if second_transaction_required:
        with transaction.atomic(savepoint=False):
            # Generate a common query to avoid n+1 selections
            #   https://github.com/jazzband/django-simple-history/issues/974
            cumulative_filter = None
            obj_when_list = []
            for i, obj in enumerate(objs_with_id):
                attributes = dict(
                    filter(
                        lambda x: x[1] is not None,
                        model_to_dict(obj, exclude=exclude_fields).items(),
                    )
                )
                q = Q(**attributes)
                cumulative_filter = (cumulative_filter | q) if cumulative_filter else q
                # https://stackoverflow.com/a/49625179/1960509
                # DEV: If an attribute has `then` as a key
                #   then they'll also run into issues with `bulk_update`
                #   due to shared implementation
                #   https://github.com/django/django/blob/4.0.4/django/db/models/query.py#L624-L638
                obj_when_list.append(When(**attributes, then=i))
            obj_list = (
                list(
                    model_manager.filter(cumulative_filter).order_by(
                        Case(*obj_when_list)
                    )
                )
                if objs_with_id
                else []
            )
            history_manager.bulk_history_create(
                obj_list,
                batch_size=batch_size,
                default_user=default_user,
                default_change_reason=default_change_reason,
                default_date=default_date,
            )
        objs_with_id = obj_list
    return objs_with_id


def bulk_update_with_history(
    objs,
    model,
    fields,
    batch_size=None,
    default_user=None,
    default_change_reason=None,
    default_date=None,
    manager=None,
):
    """
    Bulk update the objects specified by objs while also bulk creating
    their history (all in one transaction).
    :param objs: List of objs of type model to be updated
    :param model: Model class that should be updated
    :param fields: The fields that are updated
    :param batch_size: Number of objects that should be updated in each batch
    :param default_user: Optional user to specify as the history_user in each historical
        record
    :param default_change_reason: Optional change reason to specify as the change_reason
        in each historical record
    :param default_date: Optional date to specify as the history_date in each historical
        record
    :param manager: Optional model manager to use for the model instead of the default
        manager
    """
    history_manager = get_history_manager_for_model(model)
    model_manager = manager or model._default_manager
    if model_manager.model is not model:
        raise AlternativeManagerError("The given manager does not belong to the model.")

    with transaction.atomic(savepoint=False):
        model_manager.bulk_update(objs, fields, batch_size=batch_size)
        history_manager.bulk_history_create(
            objs,
            batch_size=batch_size,
            update=True,
            default_user=default_user,
            default_change_reason=default_change_reason,
            default_date=default_date,
        )


def get_change_reason_from_object(obj):
    if hasattr(obj, "_change_reason"):
        return getattr(obj, "_change_reason")

    return None
