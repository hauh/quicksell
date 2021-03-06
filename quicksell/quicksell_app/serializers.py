"""User serilaizers."""

from uuid import UUID

from django.contrib.auth import password_validation
from django.contrib.gis.geos import Point
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from drf_yasg.utils import swagger_serializer_method
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.fields import CharField, Field, IntegerField
from rest_framework.serializers import ModelSerializer, SerializerMethodField

from quicksell_app import models


class Base64UUIDField(Field):
	"""UUID to pretty base64 format."""

	def to_representation(self, value):
		return urlsafe_base64_encode(value.bytes)

	def to_internal_value(self, data):
		if isinstance(data, UUID):
			return data
		try:
			return UUID(bytes=urlsafe_base64_decode(data))
		except ValueError as err:
			raise NotFound() from err


class PointField(Field):
	"""Point serialization."""

	def to_representation(self, point):
		return f'{point.x}, {point.y}'

	def to_internal_value(self, coordinates):
		try:
			x, y = (float(coord) for coord in coordinates.split(','))
		except (ValueError, TypeError) as e:
			raise ValidationError("Required format: 'latitude, longitude'.") from e
		return Point(x, y)


class Location(ModelSerializer):
	"""Location serializer."""

	coordinates = PointField()

	class Meta:
		model = models.Location
		fields = 'coordinates', 'address'

	def create(self, validated_data):
		coords = validated_data.pop('coordinates')
		location, _ = models.Location.objects.get_or_create(coordinates=coords)
		return super().update(location, validated_data)

	def update(self, location, validated_data):
		coords = validated_data.pop('coordinates', None)
		if location.coordinates != coords:
			location, _ = models.Location.objects.get_or_create(coordinates=coords)
		return super().update(location, validated_data)


class Profile(ModelSerializer):
	"""Users' Profile info."""

	uuid = Base64UUIDField(read_only=True)
	location = Location(required=False)

	class Meta:
		model = models.Profile
		fields = (
			'uuid', 'date_created', 'full_name', 'about',
			'online', 'rating', 'avatar', 'location'
		)
		read_only_fields = 'uuid', 'date_created', 'online', 'rating'

	def update(self, profile, validated_data):
		if location_data := validated_data.pop('location', None):
			profile.location = Location().update(profile.location, location_data)
		return super().update(profile, validated_data)


class User(ModelSerializer):
	"""Users's account."""

	full_name = CharField(write_only=True)
	password = CharField(write_only=True)
	fcm_id = CharField(min_length=100, write_only=True)

	profile = Profile(read_only=True)

	class Meta:
		model = models.User
		fields = (
			'full_name', 'email', 'password', 'fcm_id',
			'is_email_verified', 'date_joined', 'balance', 'profile'
		)
		read_only_fields = 'is_email_verified', 'date_joined', 'balance', 'profile'

	def validate_password(self, password):
		password_validation.validate_password(password)
		return password

	def create(self, validated_data):
		fcm_id = validated_data.pop('fcm_id')
		full_name = validated_data.pop('full_name')
		with transaction.atomic():
			device, created = models.Device.objects.get_or_create(fcm_id=fcm_id)
			if not created:
				device.is_active = True
				device.fails_count = 0
				device.save()
				return device.owner
			user = models.User.objects.create_user(device=device, **validated_data)
			user.profile.full_name = full_name
			user.profile.save()
		return user


class CategoryField(Field):
	"""Listing's category."""

	def to_representation(self, category):
		return category.name

	def to_internal_value(self, category_name):
		try:
			category = models.Category.objects.get(name=category_name)
		except models.Category.DoesNotExist as err:
			raise ValidationError("Category doesn't exist.") from err
		if not category.is_leaf_node():
			raise ValidationError("Category should be at lowest level.")
		return category


class Listing(ModelSerializer):
	"""Listing info."""

	uuid = Base64UUIDField(read_only=True)
	price = IntegerField(min_value=0)
	seller = Profile(read_only=True)
	category = CategoryField()
	location = Location()

	class Meta:
		model = models.Listing
		fields = (
			'uuid', 'title', 'description', 'price', 'category', 'status',
			'quantity', 'sold', 'views', 'date_created', 'date_expires',
			'location', 'condition_new', 'properties', 'seller', 'photos'
		)
		depth = 1
		read_only_fields = (
			'uuid', 'sold', 'views', 'date_created',
			'date_expires', 'seller', 'shop', 'photos'
		)
		ordering = 'created'

	def create(self, validated_data):
		with transaction.atomic():
			location = Location().create(validated_data.pop('location'))
			return models.Listing.objects.create(location=location, **validated_data)

	def update(self, listing, validated_data):
		if location_data := validated_data.pop('location', None):
			listing.location = Location().update(listing.location, location_data)
		return super().update(listing, validated_data)


class Message(ModelSerializer):
	"""Chat's message serializer."""

	is_yours = SerializerMethodField()

	class Meta:
		model = models.Message
		fields = 'is_yours', 'text', 'timestamp', 'read'
		read_only_fields = 'is_yours', 'timestamp', 'read'

	def get_is_yours(self, message_object):
		return self.context['request'].user == message_object.author


class Chat(ModelSerializer):
	"""Chat serializer."""

	# POST
	to_uuid = Base64UUIDField(write_only=True)
	listing_uuid = Base64UUIDField(write_only=True)

	# GET
	uuid = Base64UUIDField(read_only=True)
	interlocutor = SerializerMethodField()
	listing = Listing(read_only=True)
	latest_message = SerializerMethodField()

	class Meta:
		model = models.Chat
		fields = (
			'to_uuid', 'listing_uuid',
			'uuid', 'subject', 'interlocutor', 'listing', 'latest_message'
		)
		read_only_fields = fields

	@swagger_serializer_method(Profile)
	def get_interlocutor(self, chat_object):
		if self.context['request'].user != chat_object.creator:
			interlocutor_profile = chat_object.creator.profile
		else:
			interlocutor_profile = chat_object.interlocutor.profile
		return Profile(interlocutor_profile, context=self.context).data

	@swagger_serializer_method(Message)
	def get_latest_message(self, chat_object):
		try:
			latest_message = chat_object.messages.latest('timestamp')
			return Message(latest_message, context=self.context).data
		except models.Message.DoesNotExist:
			return None

	def create(self, val_data):
		creator = self.context['request'].user
		to_user = get_object_or_404(models.Profile, uuid=val_data['to_uuid']).user
		listing = get_object_or_404(models.Listing, uuid=val_data['listing_uuid'])
		chat, created = models.Chat.objects.get_or_create(
			creator=creator, interlocutor=to_user, listing=listing
		)
		if created:
			chat.subject = listing.title
			chat.save()
		return chat
