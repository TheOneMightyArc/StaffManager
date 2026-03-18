
# pyright: ignore[reportMissingImports]
import discord
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, Union
import uuid

from redbot.core import commands, Config, checks
from redbot.core.utils.chat_formatting import pagify, box
from redbot.core.utils.views import ConfirmView
from discord.ext import tasks

log = logging.getLogger("red.StaffManager")

class StaffManager(commands.Cog):
    """
    Professional Staff Management System.
    
    Handles promotions, demotions, strikes, history, and a live updating Staff List.
    """

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=8877665544, force_registration=True)
        
        default_guild = {
            "setup": {
                "staff_list_channel": None,
                "log_channel": None,
                "promo_channel": None,
                "list_message_id": None
            },
            "roles": {},  # {str_role_id: {"hierarchy": int, "label": str}}
            "strikes": {}, # {str_user_id: [{"id": str, "reason": str, "issuer": int, "date": timestamp, "active": bool}]}
            "history": [], # List of past actions
            "settings": {
                "max_strikes": 3,
                "auto_demote": True,
                "show_status": True,
                "show_join_date": True,
                "embed_color": 0x2b2d31
            }
        }
        self.config.register_guild(**default_guild)
        
        # Debounce for staff list updates
        self._list_update_locks = {}
        self._list_update_pending = set()
        self.strike_cleanup_loop.start()

    def cog_unload(self):
        self.strike_cleanup_loop.cancel()

    # =========================================================================
    # UTILITIES
    # =========================================================================

    async def get_staff_roles(self, guild: discord.Guild):
        """Returns a sorted list of configured staff roles."""
        roles_data = await self.config.guild(guild).roles()
        # Sort by hierarchy (Descending: Higher number = Higher Rank)
        sorted_roles = sorted(roles_data.items(), key=lambda x: x[1]['hierarchy'], reverse=True)
        
        results = []
        for rid, data in sorted_roles:
            role = guild.get_role(int(rid))
            if role:
                results.append((role, data))
        return results

    async def log_action(self, guild: discord.Guild, embed: discord.Embed):
        """Sends a log embed to the configured channel."""
        cid = await self.config.guild(guild).setup.get_raw("log_channel")
        if cid:
            channel = guild.get_channel(cid)
            if channel:
                try:
                    await channel.send(embed=embed)
                except:
                    pass

    async def add_history(self, guild: discord.Guild, user: discord.Member, action: str, moderator: discord.Member, details: str):
        """Adds an entry to the audit history."""
        entry = {
            "user_id": user.id,
            "username": user.name,
            "action": action,
            "mod_id": moderator.id,
            "mod_name": moderator.name,
            "details": details,
            "timestamp": datetime.utcnow().timestamp()
        }
        async with self.config.guild(guild).history() as history:
            history.insert(0, entry)
            # Keep history manageable
            if len(history) > 500:
                history.pop()

    # =========================================================================
    # STAFF LIST LOGIC
    # =========================================================================

    async def update_staff_list(self, guild: discord.Guild):
        """Generates and updates the live staff list message."""
        # Simple debounce
        if guild.id in self._list_update_locks:
            # Queue a follow-up refresh so updates are not dropped while locked.
            self._list_update_pending.add(guild.id)
            return
        self._list_update_locks[guild.id] = True
        
        try:
            while True:
                self._list_update_pending.discard(guild.id)

                # Wait a moment to batch rapid changes
                await asyncio.sleep(5)

                data = await self.config.guild(guild).all()
                channel_id = data['setup']['staff_list_channel']
                msg_id = data['setup']['list_message_id']

                if channel_id:
                    channel = guild.get_channel(channel_id)
                    if channel:
                        embed = discord.Embed(
                            title=f"🛡️ {guild.name} Staff Team",
                            color=data['settings']['embed_color'],
                            timestamp=datetime.utcnow()
                        )

                        # Use specific image if configured (placeholder logic)
                        if guild.icon:
                            embed.set_thumbnail(url=guild.icon.url)

                        staff_roles = await self.get_staff_roles(guild)

                        total_staff = 0

                        for role, rdata in staff_roles:
                            # Get members with this role
                            members = [m for m in role.members]

                            # Filter: If a member has multiple staff roles, only show them in the HIGHEST hierarchy one
                            # This requires checking against all other configured staff roles
                            unique_members = []
                            for m in members:
                                highest_hierarchy = 0
                                highest_role_id = None

                                # Find user's actual highest staff role
                                for sr_role, sr_data in staff_roles:
                                    if sr_role in m.roles:
                                        if sr_data['hierarchy'] > highest_hierarchy:
                                            highest_hierarchy = sr_data['hierarchy']
                                            highest_role_id = sr_role.id

                                if highest_role_id == role.id:
                                    unique_members.append(m)

                            if not unique_members:
                                continue

                            # Sort by status priority (Online > Idle > DND > Offline)
                            def sort_key(m):
                                status_order = {"online": 0, "idle": 1, "dnd": 2, "offline": 3}
                                return (status_order.get(str(m.status), 3), m.joined_at or datetime.utcnow())

                            unique_members.sort(key=sort_key)
                            total_staff += len(unique_members)

                            lines = []
                            for m in unique_members:
                                status_emojis = {
                                    "online": "🟢", "idle": "🌙", "dnd": "🔴", "offline": "⚫", "streaming": "🟣"
                                }
                                emoji = status_emojis.get(str(m.status), "⚫")

                                line = f"{m.mention}"
                                if data['settings']['show_status']:
                                    line = f"{emoji} {line}"
                                if data['settings']['show_join_date']:
                                    line += f" `Joined: {m.joined_at.strftime('%Y-%m-%d')}`"
                                lines.append(line)

                            embed.add_field(name=f"{rdata['label']} ({len(unique_members)})", value="\n".join(lines), inline=False)

                        embed.set_footer(text=f"Total Staff: {total_staff}")

                        # Send or Edit
                        if msg_id:
                            try:
                                msg = await channel.fetch_message(msg_id)
                                await msg.edit(embed=embed)
                            except discord.NotFound:
                                msg = await channel.send(embed=embed)
                                await self.config.guild(guild).setup.list_message_id.set(msg.id)
                        else:
                            msg = await channel.send(embed=embed)
                            await self.config.guild(guild).setup.list_message_id.set(msg.id)

                # If another refresh was requested while we were processing, run once more.
                if guild.id not in self._list_update_pending:
                    break

        finally:
            self._list_update_locks.pop(guild.id, None)
            self._list_update_pending.discard(guild.id)

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        # Trigger update if roles changed
        if before.roles != after.roles:
            await self.update_staff_list(after.guild)

    @commands.Cog.listener()
    async def on_presence_update(self, before, after):
        # Trigger update if status changed (and user is staff)
        # To avoid spam, we check if they have a staff role first
        roles_data = await self.config.guild(after.guild).roles()
        is_staff = any(str(r.id) in roles_data for r in after.roles)
        
        if is_staff and str(before.status) != str(after.status):
            await self.update_staff_list(after.guild)

    # =========================================================================
    # PROMOTION / DEMOTION COMMANDS
    # =========================================================================

    @commands.group()
    @commands.guild_only()
    async def staff(self, ctx):
        """Staff Management commands."""
        pass

    @staff.command(name="promote")
    @checks.admin_or_permissions(manage_roles=True)
    async def staff_promote(self, ctx, member: discord.Member, role: Optional[discord.Role] = None, *, reason: str = "No reason provided"):
        """
        Promote a staff member.
        If no role is specified, it promotes to the next hierarchy level.
        """
        staff_roles = await self.get_staff_roles(ctx.guild) # Sorted Descending
        if not staff_roles:
            return await ctx.send("No staff roles configured. Use `[p]staffset addrole` first.")

        # Determine current rank
        current_index = -1
        current_role_obj = None
        
        for i, (r, data) in enumerate(staff_roles):
            if r in member.roles:
                # Since staff_roles is sorted High->Low, finding the first match is usually their "highest" rank
                current_index = i
                current_role_obj = r
                break

        target_role = None
        
        if role:
            # Manual target
            target_role = role
            # Validation
            if str(role.id) not in await self.config.guild(ctx.guild).roles():
                return await ctx.send("That role is not configured as a Staff Role.")
        else:
            # Auto promote (Move UP the hierarchy, which means Lower index in our High->Low list)
            if current_index == -1:
                # Not staff, give lowest rank (Last index)
                target_role = staff_roles[-1][0]
            elif current_index == 0:
                return await ctx.send(f"{member.mention} is already at the highest rank!")
            else:
                target_role = staff_roles[current_index - 1][0]

        # Confirmation
        view = ConfirmView(ctx.author)
        msg = await ctx.send(
            f"**Promotion Request**\n"
            f"User: {member.mention}\n"
            f"Current: {current_role_obj.mention if current_role_obj else 'None'}\n"
            f"Target: {target_role.mention}\n"
            f"Reason: {reason}",
            view=view
        )
        await view.wait()
        
        if view.result:
            try:
                await member.add_roles(target_role, reason=f"Promoted by {ctx.author}: {reason}")
                if current_role_obj and current_role_obj != target_role:
                    await member.remove_roles(current_role_obj, reason="Removing old rank")
                
                await msg.edit(content="✅ **Promotion Successful**", view=None)
                
                # Logs
                await self.add_history(ctx.guild, member, "Promotion", ctx.author, f"To {target_role.name}: {reason}")
                
                embed = discord.Embed(title="📈 Staff Promotion", color=discord.Color.green())
                embed.add_field(name="User", value=f"{member} ({member.id})")
                embed.add_field(name="Promoted To", value=target_role.mention)
                embed.add_field(name="Promoted By", value=ctx.author.mention)
                embed.add_field(name="Reason", value=reason)
                await self.log_action(ctx.guild, embed)
                
                # Channel Announcement
                promo_chan_id = await self.config.guild(ctx.guild).setup.promo_channel()
                if promo_chan_id:
                    c = ctx.guild.get_channel(promo_chan_id)
                    if c:
                        await c.send(f"🎉 Congrats to {member.mention} on their promotion to **{target_role.name}**!")

                await self.update_staff_list(ctx.guild)

            except discord.Forbidden:
                await msg.edit(content="❌ I do not have permission to manage roles.", view=None)
        else:
            await msg.edit(content="❌ Action cancelled.", view=None)

    @staff.command(name="demote")
    @checks.admin_or_permissions(manage_roles=True)
    async def staff_demote(self, ctx, member: discord.Member, role: Optional[discord.Role] = None, *, reason: str = "No reason provided"):
        """
        Demote a staff member.
        If no role is specified, it demotes to the previous hierarchy level.
        """
        staff_roles = await self.get_staff_roles(ctx.guild) # Sorted Descending
        if not staff_roles:
            return await ctx.send("No staff roles configured.")

        current_index = -1
        current_role_obj = None
        
        for i, (r, data) in enumerate(staff_roles):
            if r in member.roles:
                current_index = i
                current_role_obj = r
                break

        if current_index == -1:
            return await ctx.send("This user does not hold any staff roles.")

        target_role = None
        
        if role:
            target_role = role
        else:
            # Auto demote (Move DOWN hierarchy => Higher Index)
            if current_index == len(staff_roles) - 1:
                # At bottom, remove from staff completely
                target_role = None 
            else:
                target_role = staff_roles[current_index + 1][0]

        # Confirmation
        target_str = target_role.mention if target_role else "**Removed from Staff**"
        
        view = ConfirmView(ctx.author)
        msg = await ctx.send(
            f"**Demotion Request**\n"
            f"User: {member.mention}\n"
            f"Current: {current_role_obj.mention}\n"
            f"Target: {target_str}\n"
            f"Reason: {reason}",
            view=view
        )
        await view.wait()
        
        if view.result:
            try:
                await member.remove_roles(current_role_obj, reason=f"Demoted by {ctx.author}: {reason}")
                if target_role:
                    await member.add_roles(target_role, reason="Demotion adjustment")
                
                await msg.edit(content="✅ **Demotion Successful**", view=None)
                
                # Logs
                await self.add_history(ctx.guild, member, "Demotion", ctx.author, f"From {current_role_obj.name}: {reason}")
                
                embed = discord.Embed(title="📉 Staff Demotion", color=discord.Color.red())
                embed.add_field(name="User", value=f"{member} ({member.id})")
                embed.add_field(name="Demoted To", value=target_role.name if target_role else "None")
                embed.add_field(name="Demoted By", value=ctx.author.mention)
                embed.add_field(name="Reason", value=reason)
                await self.log_action(ctx.guild, embed)
                
                # Channel Announcement
                promo_chan_id = await self.config.guild(ctx.guild).setup.promo_channel()
                if promo_chan_id:
                    c = ctx.guild.get_channel(promo_chan_id)
                    if c:
                        await c.send(f"⚠️ {member.mention} has been demoted from **{current_role_obj.name}**.")

                await self.update_staff_list(ctx.guild)

            except discord.Forbidden:
                await msg.edit(content="❌ I do not have permission to manage roles.", view=None)
        else:
            await msg.edit(content="❌ Action cancelled.", view=None)

    # =========================================================================
    # STRIKE SYSTEM
    # =========================================================================

    @staff.group()
    @checks.admin_or_permissions(manage_messages=True)
    async def strike(self, ctx):
        """Manage staff strikes."""
        pass

    @strike.command(name="add")
    async def strike_add(self, ctx, member: discord.Member, *, reason: str):
        """Issue a strike to a staff member."""
        strike_id = str(uuid.uuid4())[:8].upper()
        strike_data = {
            "id": strike_id,
            "reason": reason,
            "issuer": ctx.author.id,
            "date": datetime.utcnow().timestamp(),
            "active": True
        }

        async with self.config.guild(ctx.guild).strikes() as s:
            if str(member.id) not in s:
                s[str(member.id)] = []
            s[str(member.id)].append(strike_data)

        # Calculate total active
        active_strikes = [x for x in await self.config.guild(ctx.guild).strikes.get_raw(str(member.id)) if x['active']]
        count = len(active_strikes)
        max_s = await self.config.guild(ctx.guild).settings.max_strikes()

        await ctx.send(f"🚩 Strike issued to {member.mention}. (Total Active: {count}/{max_s})")
        
        # Log
        embed = discord.Embed(title="🚩 Staff Strike Issued", color=discord.Color.orange())
        embed.add_field(name="User", value=member.mention)
        embed.add_field(name="Reason", value=reason)
        embed.add_field(name="Strike ID", value=strike_id)
        embed.add_field(name="Count", value=f"{count}/{max_s}")
        await self.log_action(ctx.guild, embed)

        # DM
        try:
            await member.send(f"You received a staff strike in **{ctx.guild.name}**.\nReason: {reason}\nTotal: {count}/{max_s}")
        except:
            pass

        # Auto Demote Check
        if count >= max_s and await self.config.guild(ctx.guild).settings.auto_demote():
            await ctx.send(f"🚨 **{member.display_name}** has reached the maximum strikes. Initiating removal...")
            # Trigger removal logic (simplified here)
            staff_roles = await self.get_staff_roles(ctx.guild)
            to_remove = [r for r, d in staff_roles if r in member.roles]
            if to_remove:
                try:
                    await member.remove_roles(*to_remove, reason="Max Strikes Reached")
                    await ctx.send("User has been removed from staff roles.")
                    await self.update_staff_list(ctx.guild)
                except:
                    await ctx.send("Failed to auto-remove roles (Permissions error).")

    @strike.command(name="list")
    async def strike_list(self, ctx, member: discord.Member):
        """List strikes for a user."""
        all_strikes = await self.config.guild(ctx.guild).strikes.get_raw(str(member.id), default=[])
        if not all_strikes:
            return await ctx.send("This user has no strikes.")

        text = ""
        for s in all_strikes:
            status = "Active" if s['active'] else "Inactive"
            date_str = datetime.fromtimestamp(s['date']).strftime('%Y-%m-%d')
            text += f"`{s['id']}` - **{date_str}**: {s['reason']} ({status})\n"

        embed = discord.Embed(title=f"Strikes for {member.display_name}", description=text, color=discord.Color.blue())
        await ctx.send(embed=embed)

    @strike.command(name="remove")
    async def strike_remove(self, ctx, member: discord.Member, strike_id: str):
        """Remove (deactivate) a strike."""
        async with self.config.guild(ctx.guild).strikes() as s:
            if str(member.id) not in s:
                return await ctx.send("User has no strikes.")
            
            found = False
            for strike in s[str(member.id)]:
                if strike['id'] == strike_id:
                    strike['active'] = False
                    found = True
                    break
            
            if found:
                await ctx.send(f"Strike `{strike_id}` has been deactivated.")
                # Log
                embed = discord.Embed(title="🏳️ Staff Strike Removed", color=discord.Color.blue())
                embed.add_field(name="User", value=member.mention)
                embed.add_field(name="Removed By", value=ctx.author.mention)
                embed.add_field(name="Strike ID", value=strike_id)
                await self.log_action(ctx.guild, embed)
            else:
                await ctx.send("Strike ID not found.")

    # =========================================================================
    # CONFIGURATION
    # =========================================================================

    @commands.group()
    @commands.guild_only()
    @checks.admin_or_permissions(administrator=True)
    async def staffset(self, ctx):
        """Configure the Staff Management system."""
        pass

    @staffset.command(name="addrole")
    async def ss_addrole(self, ctx, role: discord.Role, hierarchy: int, *, label: str):
        """
        Add a role to the staff hierarchy.
        Hierarchy: Higher number = Higher Rank.
        Label: What shows on the Staff List (e.g. "Moderators").
        """
        async with self.config.guild(ctx.guild).roles() as r:
            r[str(role.id)] = {"hierarchy": hierarchy, "label": label}
        await ctx.send(f"Added {role.name} as **{label}** (Level {hierarchy}).")
        await self.update_staff_list(ctx.guild)

    @staffset.command(name="removerole")
    async def ss_removerole(self, ctx, role: discord.Role):
        """Remove a role from management."""
        async with self.config.guild(ctx.guild).roles() as r:
            if str(role.id) in r:
                del r[str(role.id)]
                await ctx.send(f"Removed {role.name} from config.")
            else:
                await ctx.send("That role is not configured.")
        await self.update_staff_list(ctx.guild)

    @staffset.command(name="channels")
    async def ss_channels(self, ctx, list_channel: Optional[discord.TextChannel], log_channel: Optional[discord.TextChannel], promo_channel: Optional[discord.TextChannel]):
        """Set the channels. Use 'None' to skip a channel."""
        async with self.config.guild(ctx.guild).setup() as s:
            if list_channel: s['staff_list_channel'] = list_channel.id
            if log_channel: s['log_channel'] = log_channel.id
            if promo_channel: s['promo_channel'] = promo_channel.id
        await ctx.send("Channels updated.")
        if list_channel:
            await self.update_staff_list(ctx.guild)

    @staffset.command(name="settings")
    async def ss_settings(self, ctx, max_strikes: int = 3, auto_demote: bool = True):
        """Configure general settings."""
        async with self.config.guild(ctx.guild).settings() as s:
            s['max_strikes'] = max_strikes
            s['auto_demote'] = auto_demote
        await ctx.send(f"Settings updated: Max Strikes: {max_strikes}, Auto Demote: {auto_demote}")

    @staffset.command(name="refresh")
    async def ss_refresh(self, ctx):
        """Force refresh the Staff List."""
        await self.update_staff_list(ctx.guild)
        await ctx.tick()

    # =========================================================================
    # TASKS
    # =========================================================================

    @tasks.loop(hours=24)
    async def strike_cleanup_loop(self):
        """Optional: Clean up very old inactive strikes."""
        # This basic implementation just runs; expand to delete old strikes if needed.
        pass

    @strike_cleanup_loop.before_loop
    async def before_cleanup(self):
        await self.bot.wait_until_ready()