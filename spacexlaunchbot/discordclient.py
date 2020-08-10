import asyncio
import logging
import platform
import signal
from typing import Union

import discord
import discordhealthcheck

from . import apis
from . import commands
from . import config
from . import notifications
from . import embeds
from . import storage


class SpaceXLaunchBotClient(discord.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        logging.info("Client initialised")

        self.ds = storage.DataStore(config.PICKLE_DUMP_LOCATION)
        logging.info("Data storage initialised")

        if platform.system() == "Linux":
            self.loop.add_signal_handler(
                signal.SIGTERM, lambda: self.loop.create_task(self.shutdown())
            )
            logging.info("Signal handler for SIGTERM registered")

        self.loop.create_task(notifications.notification_task(self))
        discordhealthcheck.start(self)

    async def on_ready(self) -> None:
        logging.info("Connected to Discord API")
        await self.set_playing(config.BOT_GAME)
        await self.update_website_metrics()

    async def shutdown(self) -> None:
        """Saves data to disk, cancels asyncio tasks, and disconnects from Discord"""
        logging.info("Shutting down")
        self.ds.save()
        for task in asyncio.Task.all_tasks():
            task.cancel()
        await self.close()

    async def update_website_metrics(self) -> None:
        """Update Discord bot websites with guild count"""
        guild_count = len(self.guilds)
        logging.info(f"Updating bot lists with a guild_count of {guild_count}")
        await apis.bot_lists.post_all_bot_lists(guild_count)

    async def on_guild_join(self, guild: discord.guild) -> None:
        logging.info(f"Joined guild, ID: {guild.id}")
        await self.update_website_metrics()

    async def on_guild_remove(self, guild: discord.guild) -> None:
        logging.info(f"Removed from guild, ID: {guild.id}")
        await self.update_website_metrics()
        # Any subscribed channels from this guild will be removed later by
        # send_notification_to_all_subscribed.

    async def set_playing(self, title: str) -> None:
        await self.change_presence(activity=discord.Game(name=title))

    async def on_message(self, message: discord.message) -> None:
        if message.author.bot or not message.guild:
            return

        message_parts = message.content.lower().split(" ")
        if message_parts[0] != config.BOT_COMMAND_PREFIX:
            return

        to_send = None

        try:
            command_used = message_parts[1]
            run_command = commands.CMD_LOOKUP[command_used]
            to_send = await run_command(
                client=self, message=message, operands=message_parts[2:]
            )

        except (KeyError, IndexError):
            pass  # Message contained wrong or no command
        except TypeError:
            logging.exception(f"run_command TypeError: {message.content=}")

        if to_send is None:
            return

        await self._send_s(message.channel, to_send)

    @staticmethod
    async def _send_s(
        channel: discord.TextChannel, to_send: Union[str, discord.Embed]
    ) -> None:
        """Safely send a text / embed message to a channel. Logs any errors that occur.

        Args:
            channel: A discord.Channel object.
            to_send: A String or discord.Embed object.

        """
        try:
            if isinstance(to_send, discord.Embed):
                if embeds.embed_is_valid(to_send):
                    await channel.send(embed=to_send)
                else:
                    logging.warning("Embed is too large to send")
            else:
                await channel.send(to_send)

        except discord.errors.Forbidden:
            # TODO: Count how many times this happens and unsub when n have happened?
            pass

        except discord.errors.HTTPException as ex:
            # Length/size is most likely cause,
            # see https://discord.com/developers/docs/resources/channel#embed-limits
            logging.warning(f"HTTPException: {ex}")

    async def send_notification_to_all_subscribed(
        self,
        to_send: Union[str, discord.Embed],
        sending_notification_type: notifications.NotificationType,
    ) -> None:
        """Send a notification message to all channels subscribed to the given type.

        Args:
            to_send: A String or discord.Embed object.
            sending_notification_type: The type of notification being sent.

        """
        channel_ids = self.ds.get_subbed_channels()
        invalid_ids = set()

        for channel_id in channel_ids:
            subscription_opts = channel_ids[channel_id]

            if (
                subscription_opts["type"] != notifications.NotificationType.all
                and subscription_opts["type"] != sending_notification_type
            ):
                continue

            channel = self.get_channel(channel_id)
            if channel is None:
                invalid_ids.add(channel_id)
                continue

            await self._send_s(channel, to_send)

            if sending_notification_type == notifications.NotificationType.launch:
                mentions = subscription_opts.get("mentions", "")
                if mentions != "":
                    await self._send_s(channel, mentions)

        for cid in invalid_ids:
            self.ds.remove_subbed_channel(cid)
