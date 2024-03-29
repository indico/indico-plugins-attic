# This file is part of the Indico plugins.
# Copyright (C) 2002 - 2021 CERN
#
# The Indico plugins are free software; you can redistribute
# them and/or modify them under the terms of the MIT License;
# see the LICENSE file for more details.

from datetime import timedelta

from celery.schedules import crontab

from indico.core.celery import celery
from indico.core.db import db
from indico.core.plugins import get_plugin_template_module
from indico.modules.events import Event
from indico.modules.vc.models.vc_rooms import VCRoom, VCRoomEventAssociation, VCRoomStatus
from indico.modules.vc.notifications import _send
from indico.util.date_time import now_utc
from indico.util.iterables import committing_iterator

from indico_vc_vidyo.api import APIException, RoomNotFoundAPIException


def find_old_vidyo_rooms(max_room_event_age):
    """Finds all Vidyo rooms that are:
       - linked to no events
       - linked only to events whose start date precedes today - max_room_event_age days
    """
    recently_used = (db.session.query(VCRoom.id)
                     .filter(VCRoom.type == 'vidyo',
                             Event.end_dt > (now_utc() - timedelta(days=max_room_event_age)))
                     .join(VCRoom.events)
                     .join(VCRoomEventAssociation.event)
                     .group_by(VCRoom.id))

    # non-deleted rooms with no recent associations
    return VCRoom.query.filter(VCRoom.status != VCRoomStatus.deleted, ~VCRoom.id.in_(recently_used)).all()


def notify_owner(plugin, vc_room):
    """Notifies about the deletion of a Vidyo room from the Vidyo server."""
    user = vc_room.vidyo_extension.owned_by_user
    tpl = get_plugin_template_module('emails/remote_deleted.html', plugin=plugin, vc_room=vc_room, event=None,
                                     vc_room_event=None, user=user)
    _send('delete', user, plugin, None, vc_room, tpl)


@celery.periodic_task(run_every=crontab(minute='0', hour='3', day_of_week='monday'), plugin='vc_vidyo')
def vidyo_cleanup(dry_run=False):
    from indico_vc_vidyo.plugin import VidyoPlugin
    max_room_event_age = VidyoPlugin.settings.get('num_days_old')

    VidyoPlugin.logger.info('Deleting Vidyo rooms that are not used or linked to events all older than %d days',
                            max_room_event_age)
    candidate_rooms = find_old_vidyo_rooms(max_room_event_age)
    VidyoPlugin.logger.info('%d rooms found', len(candidate_rooms))

    if dry_run:
        for vc_room in candidate_rooms:
            VidyoPlugin.logger.info('Would delete Vidyo room %s from server', vc_room)
        return

    for vc_room in committing_iterator(candidate_rooms, n=20):
        try:
            VidyoPlugin.instance.delete_room(vc_room, None)
            VidyoPlugin.logger.info('Room %s deleted from Vidyo server', vc_room)
            notify_owner(VidyoPlugin.instance, vc_room)
            vc_room.status = VCRoomStatus.deleted
        except RoomNotFoundAPIException:
            VidyoPlugin.logger.warning('Room %s had been already deleted from the Vidyo server', vc_room)
            vc_room.status = VCRoomStatus.deleted
        except APIException:
            VidyoPlugin.logger.exception('Impossible to delete Vidyo room %s', vc_room)
