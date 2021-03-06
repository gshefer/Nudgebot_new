import os
from random import choice

from jinja2 import Template
from celery.schedules import crontab
from nudgebot.tasks.base import ConditionalTask
from nudgebot.thirdparty.github.base import Github
from nudgebot.thirdparty.github.pull_request import PullRequest
from nudgebot.thirdparty.github.repository import Repository
from nudgebot.utils import send_email, getnode
from nudgebot.tasks.base import PeriodicTask
from nudgebot.thirdparty.irc.base import IRCendpoint
from nudgebot.thirdparty.irc.bot import MessageMentionedMeEvent
from nudgebot.thirdparty.irc.message import Message
from nudgebot.settings import CurrentProject
from nudgebot.config.user import User


# Create your tasks here


class SetReviewerWhenMovedToRFR(ConditionalTask):
    """This task adding a reviewer once the title includes an '[RFR]' tag"""

    Endpoint = Github()                        # The third party Endpoint for this task is Github.
    EndpointScope = PullRequest                # The scope of this task is pull request.
    NAME = 'SetReviewerWhenMovedToRFR'         # The name of the task.

    @property
    def condition(self):
        # We get the `title_tags` and `reviewers` statistics to check whether the title tag has moved
        # to RFR and that there are no reviewers that have been assigned.
        return (
            'RFR' in self.statistics.my_pulls_statistics.title_tags and
            not self.statistics.my_pulls_statistics.reviewers
        )

    def get_data(self):
        """Collecting data"""
        repos_data = next(repo for repo in CurrentProject().config.config.github.repositories
                          if repo.name == self.statistics.my_repo_statistics.repository)
        maintainers = repos_data.maintainers
        reviewer = choice(maintainers)
        owner = self.statistics.my_pulls_statistics.owner
        reviewer_contact = User.get_user(github_login=reviewer)
        owner_contact = User.get_user(github_login=owner) or owner
        return owner_contact, reviewer_contact

    def get_artifacts(self):
        return self.statistics.my_pulls_statistics.title_tags

    def run(self):
        """Running the task"""
        owner_contact, reviewer_contact = self.get_data()
        owner_contact = (owner_contact.irc_nick if isinstance(owner_contact, User) else owner_contact)
        self.scopes[PullRequest].add_reviewers(reviewer_contact.github)


class AlertOnMergedEvent(ConditionalTask):
    """Alert when some pull request has been merged"""

    Endpoint = Github()                        # The third party Endpoint for this task is Github.
    EndpointScope = Repository                 # The scope of this task is pull request.
    NAME = 'AlertOnMergedEvent'                # The name of the task.

    @property
    def condition(self):
        return (
            self.event and self.event.data['type'] == 'PullRequestEvent' and
            getnode(self.event.data, ['payload', 'action']) == 'closed' and
            getnode(self.event.data, ['payload', 'pull_request', 'merged'])
        )

    def get_artifacts(self):
        return [str(self.event.data['id'])]

    def run(self):
        actor = self.event.data['sender']['login']
        number = self.event.data['payload']['pull_request']['number']
        IRCendpoint().client.msg('##bot-testing', f'{actor} has merged PR#{number}')


class AlertOnMentionedUser(ConditionalTask):
    """This task prompt the user in IRC once he was mentioned in some pull request."""

    Endpoint = Github()
    EndpointScope = PullRequest
    NAME = 'AlertOnMentionedUser'
    RUN_ONCE = False

    def get_artifacts(self):
        return [str(self.event.data['id'])]

    @property
    def condition(self):
        """
        Checking that the task triggered by event, the event is a comment event
        and there are mentioned users in the comment.
        """
        return bool(self.event and self.event.artifacts and
                    self.event.artifacts.get('comment') and self.event.artifacts['comment'].mentioned_users)

    def run(self):
        mentioned, actor = self.event.artifacts['comment'].mentioned_users, self.event.artifacts['actor']
        # Getting IRC nick in case that the actor in the users list
        user = User.get_user(github_login=actor.login)
        actor = (user.irc_nick if user else actor.login)
        for mentioned_user in mentioned:
            # Getting IRC nick in case that the user in the users list
            user = User.get_user(github_login=mentioned_user.login)
            mentioned_user = (user.irc_nick if user else mentioned_user.login)
            # Composing the comment from the statistics, mentioned users and actor
            IRCendpoint().client.msg(
                '##bot-testing', f'{mentioned_user}, {actor} has '
                f'mentioned you in {self.statistics.my_repo_statistics.organization}/'
                f'{self.statistics.my_repo_statistics.repository} '
                f'@ PR#{self.statistics.my_pulls_statistics.issue_number}.'
            )


class IRCAnswerQuestion(ConditionalTask):
    """This task answer once someone send message to the bot in IRC."""

    Endpoint = IRCendpoint()      # The third party Endpoint for this task is IRC.
    EndpointScope = Message       # The scope of this task is pull request.
    NAME = 'IRCAnswerQuestion'    # The name of the task.
    RUN_ONCE = False              # Indicate that the task will always run. not only in the first occurrence.

    @property
    def condition(self):
        return self.event and isinstance(self.event, MessageMentionedMeEvent)

    def run(self):
        me = self.Endpoint.client.nick
        content, sender, channel = self.scope.content, self.scope.sender, self.scope.channel

        def answer(content):
            return self.Endpoint.client.msg(channel.name, f'{sender}, {content}')

        if f'{me}, ping' == content:
            answer('pong')
        elif f'{me}, #pr' == content:
            answer(', '.join([
                f'{repo.name}: {repo.number_of_open_issues}'
                for repo in self.all_statistics.github_repository
            ]))
        else:
            answer(f'Unknown option "{content}"')
            self.Endpoint.client.msg(channel.name, 'options:')
            self.Endpoint.client.msg(channel.name, '    ping - Get pong back.')
            self.Endpoint.client.msg(channel.name, '    #pr - Number of open pull requests per repository.')


class DailyReport(PeriodicTask):
    """This task is a periodic task. It sends a report to the maintainers every day at 12:00AM."""

    NAME = 'DailyReport'
    CRONTAB = crontab(hour=12)

    def get_report(self):
        data = {}

        for pr_stats in self.all_statistics.github_pull_request:
            if pr_stats.repository not in data:
                data[pr_stats.repository] = {}
            data[pr_stats.repository][pr_stats.issue_number] = pr_stats
            data[pr_stats.repository][pr_stats.issue_number].update({
                'comments': pr_stats.total_comments,
                'commits': pr_stats.number_of_commits,
                'reviewers': pr_stats.reviewers
            })
        with open(os.path.join(os.path.dirname(__file__), 'daily_report.j2'), 'r') as t:
            template = Template(t.read())
        return template.render(data=data)

    def run(self):
        maintainers_emails = set()
        for repo_data in CurrentProject().config.config.github.repositories:
            for maintainer in repo_data.maintainers:
                for user_data in CurrentProject().config.users:
                    if user_data.github_login == maintainer:
                        maintainers_emails.add(user_data.email)

        send_email(CurrentProject().config.credentials.email.address, list(maintainers_emails),
                   'Daily report', self.get_report(), text_format='html')
