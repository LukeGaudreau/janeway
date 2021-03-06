# -*- coding: utf-8 -*-
# Generated by Django 1.9.13 on 2017-07-11 12:03
from __future__ import unicode_literals

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('contenttypes', '0002_remove_content_type_name'),
    ]

    operations = [
        migrations.CreateModel(
            name='NavigationItem',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('object_id', models.PositiveIntegerField(blank=True, null=True)),
                ('link_name', models.CharField(max_length=100)),
                ('link', models.CharField(max_length=100)),
                ('is_external', models.BooleanField(default=False)),
                ('sequence', models.IntegerField(default=99)),
                ('has_sub_nav', models.BooleanField(default=False, verbose_name='Has Sub Navigation')),
                ('content_type', models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, related_name='nav_content', to='contenttypes.ContentType')),
            ],
        ),
        migrations.CreateModel(
            name='Page',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('object_id', models.PositiveIntegerField(blank=True, null=True)),
                ('name', models.CharField(help_text='Page name displayed in the URL bar eg. about or contact', max_length=300)),
                ('display_name', models.CharField(help_text='Name of the page, max 100 chars, displayed in the nav and on the header of the page eg. About or Contact', max_length=100)),
                ('content', models.TextField(blank=True, null=True)),
                ('is_markdown', models.BooleanField(default=True)),
                ('edited', models.DateTimeField(auto_now=True)),
                ('content_type', models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, related_name='page_content', to='contenttypes.ContentType')),
            ],
        ),
        migrations.AddField(
            model_name='navigationitem',
            name='page',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, to='cms.Page'),
        ),
        migrations.AddField(
            model_name='navigationitem',
            name='top_level_nav',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, to='cms.NavigationItem', verbose_name='Top Level Nav Item'),
        ),
    ]
