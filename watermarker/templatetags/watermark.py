from datetime import datetime
from hashlib import sha1
from django.core.cache import cache
import Image
import errno
import logging
import os
import traceback

from django.conf import settings
from django import template
from watermarker import utils
from watermarker.models import Watermark, WatermarkCreatedFile

from django.core.files.base import ContentFile
    
try:
    from cStringIO import StringIO
except ImportError:
    from StringIO import StringIO

register = template.Library()

# determine the quality of the image after the watermark is applied
QUALITY = getattr(settings, 'WATERMARKING_QUALITY', 85)
OBSCURE = getattr(settings, 'WATERMARK_OBSCURE_ORIGINAL', True)
RANDOM_POS_ONCE = getattr(settings, 'WATERMARK_RANDOM_POSITION_ONCE', True)

log = logging.getLogger('watermarker')


def get_image_from_s3(name):
    if settings.DEFAULT_FILE_STORAGE ==\
            "django.core.files.storage.FileSystemStorage":
        return Image.open(settings.MEDIA_ROOT+name)
    try:
        name = name.split('?')[0]
    except KeyError:
        pass
    from storages.backends.s3boto import S3BotoStorage, S3BotoStorageFile
    cfile = S3BotoStorageFile(name=name, mode='r',
                              storage=S3BotoStorage())
    im = Image.open(cfile)
    return im

def store_image_to_s3(image, name, q, format):
    if settings.DEFAULT_FILE_STORAGE ==\
            "django.core.files.storage.FileSystemStorage":
        from django.core.files.storage import FileSystemStorage
        memory_file = StringIO()
        image.save(memory_file, quality=q, format=format)
        cf = ContentFile(memory_file.getvalue())
        storage = S3BotoStorage()
        storage.save(settings.MEDIA_ROOT+name, cf)
        return
    try:
        name = name.split('?')[0]
    except KeyError:
        pass
    from storages.backends.s3boto import S3BotoStorage, S3BotoStorageFile
    memory_file = StringIO()
    image.save(memory_file, quality=q, format=format)
    cf = ContentFile(memory_file.getvalue())
    storage = S3BotoStorage()
    storage.save(name, cf)
     

class Watermarker(object):

    def __call__(self, url, name, position=None, opacity=0.5, tile=False,
                 scale=1.0, greyscale=False, rotation=0, obscure=OBSCURE,
                 quality=QUALITY, random_position_once=RANDOM_POS_ONCE):
        """
        Creates a watermarked copy of an image.

        * ``name``: This is the name of the Watermark object that you wish to
          apply to the image.
        * ``position``:  There are several options.

            * ``R``: random placement, which is the default behavior.
            * ``C``: center the watermark
            * ``XxY`` where ``X`` is either a specific pixel position on the
              x-axis or a percentage of the total width of the target image and
              ``Y`` is a specific pixel position on the y-axis of the image or
              a percentage of the total height of the target image.  These
              values represent the location of the top and left edges of the
              watermark.  If either ``X`` or ``Y`` is a percentage, you must
              use a percent sign.  This is not used if either one of the
              ``tiled`` or ``scale`` parameters are true.

              Examples:

                * ``50%x50%``: positions the watermark at the center of the
                  image.
                * ``50%x100``: positions the watermark at the midpoint of the
                  total width of the image and 100 pixels from the top of the
                  image
                * ``100x50%``: positions the watermark at the midpoint of the
                  total height of the image and 100 pixels from the left edge
                  of the image
                * ``100x100``: positions the top-left corner of the watermark
                  at 100 pixels from the top of the image and 100 pixels from
                  the left edge of the image.

            * ``br``, ``bl``, ``tr``, ``tl`` where ``b`` means "bottom", ``t``
              means "top", ``l`` means "left", and ``r`` means "right".  This
              will position the watermark at the extreme edge of the original
              image with just enough room for the watermark to "fully show".
              This assumes the watermark is not as big as the original image.

        * ``opacity``: an integer from 0 to 100.  This value represents the
          transparency level of the watermark when it is applied.  A value of
          100 means the watermark is completely opaque while a value of 0 means
          the watermark will be invisible.
        * ``tile``: ``True`` or ``False`` to specify whether or not the
          watermark shall be tiled across the entire image.
        * ``scale``: a floating-point number above 0 to specify the scaling for
          the watermark.  If you want the watermark to be scaled to its maximum
          without falling off the edge of the target image, use ``F``.  By
          default, scale is set to ``1.0``, or 1:1 scaling, meaning the
          watermark will be placed on the target image at its original size.
        * ``greyscale``: ``True`` or ``False`` to specify whether or not the
          watermark should be converted to a greyscale image before applying it
          to the target image.  Default is ``False``.
        * ``rotation``: 0 to 359 to specify the number of degrees to rotate the
          watermark before applying it to the target image.  Alternatively, you
          may set ``rotation=R`` for a random rotation value.
        * ``obscure``: set to ``False`` if you wish to expose the original
          image's filename.  Defaults to ``True``.
        * ``quality``: the quality of the resulting watermarked image.  Default
          is 85.
        """

        # look for the specified watermark by name.  If it's not there, go no
        # further
        try:
            watermark = Watermark.objects.get(name=name, is_active=True)
        except Watermark.DoesNotExist:
            log.error('Watermark "%s" does not exist... Bailing out.' % name)
            return url

        # make sure URL is a string
        url = str(url)

        basedir = '%s/watermarked' % os.path.dirname(url)
        base, ext = os.path.splitext(os.path.basename(url))

        #cached_mark = cache.get('watermark_{0}_{1}'.format(
        #    hash(name),
        #    hash(url)), None)
        #if cached_mark:
        #    return cached_mark
        from main.main_redis import XPRedisClient
        old_mark = XPRedisClient().get_watermark_cache(name, url)
        
        if old_mark:
            return old_mark

        # open the target image file along with the watermark image
        target_path = self.get_url_path(url)
        try:
            target = get_image_from_s3(target_path)
            mark = get_image_from_s3(watermark.image.name)
        except AttributeError:
            return url
        # determine the actual value that the parameters provided will render
        random_position = bool(position is None or str(position).lower() == 'r')
        scale = utils.determine_scale(scale, target, mark)
        rotation = utils.determine_rotation(rotation, mark)
        pos = utils.determine_position(position, target, mark)

        # see if we need to create only one randomly positioned watermarked
        # image
        if not random_position or \
            (not random_position_once and random_position):
            log.debug('Generating random position for watermark each time')
            position = pos
        else:
            log.debug('Random positioning watermark once')

        params = {
            'position':  position,
            'opacity':   opacity,
            'scale':     scale,
            'tile':      tile,
            'greyscale': greyscale,
            'rotation':  rotation,
            'base':      base,
            'ext':       ext,
            'quality':   quality,
            'watermark': watermark.id,
            'opacity_int': int(opacity * 100),
            'left':      pos[0],
            'top':       pos[1],
        }
        log.debug('Params: %s' % params)

        wm_name = self.watermark_name(mark, **params)
        wm_url = self.watermark_path(basedir, base, ext, wm_name, obscure)
        wm_path = self.get_url_path(wm_url)
        XPRedisClient().set_watermark_cache(name, url, wm_url)

        log.debug('Watermark name: %s; URL: %s; Path: %s' % (
            wm_name, wm_url, wm_path
        ))

        # see if the image already exists on the filesystem.  If it does, use
        # it.
        # from storages.backends.s3boto import S3BotoStorage
        # storage = S3BotoStorage()
        # if storage.exists(wm_path):
        #     cached_mark = cache.set('watermark_{0}_{1}'.format(
        #         hash(name),
        #         hash(url)), wm_url, settings.WATERMARK_CACHE_TIMEOUT)
        #     return wm_url

        # make sure the position is in our params for the watermark
        params['position'] = pos
        
        self.create_watermark(target, mark, wm_path, **params)
        try:
            WatermarkCreatedFile.objects.get_or_create(
                watermark_name=name,
                target_path=url,
                url=wm_url)
        except WatermarkCreatedFile.MultipleObjectsReturned:
            pass
        #cached_mark = cache.set('watermark_{0}_{1}'.format(
        #        hash(name),
        #        hash(url)), wm_url, settings.WATERMARK_CACHE_TIMEOUT)
        # send back the URL to the new, watermarked image
        return wm_url

    def get_url_path(self, url, root=settings.MEDIA_ROOT,
        url_root=settings.MEDIA_URL):
        """Makes a filesystem path from the specified URL"""

        #if url.startswith(url_root):
        #    url = url[len(url_root):] # strip media root url
        try:
            url =  url.split(settings.AWS_S3_CUSTOM_DOMAIN)[1]
        except:
            pass
        if url.startswith('/'):
            url = url[1:]
        return url
        #return os.path.normpath(os.path.join(root, url))

    def watermark_name(self, mark, **kwargs):
        """Comes up with a good filename for the watermarked image"""

        params = [
            '%(base)s',
            'wm',
            'w%(watermark)i',
            'o%(opacity_int)i',
            'gs%(greyscale)i',
            'r%(rotation)i',
            '_p%(position)s',
        ]

        scale = kwargs.get('scale', None)
        if scale and scale != mark.size:
            params.append('_s%i' % (float(kwargs['scale'][0]) / mark.size[0] * 100))

        if kwargs.get('tile', None):
            params.append('_tiled')

        # make thumbnail filename
        name = '%s%s' % ('_'.join(params), kwargs['ext'])
        return name % kwargs

    def watermark_path(self, basedir, base, ext, wm_name, obscure=True):
        """Determines an appropriate watermark path"""

        hash = sha1(wm_name).hexdigest()

        # figure out where the watermark would be saved on the filesystem
        if obscure:
            log.debug('Obscuring original image name: %s => %s' % (wm_name, hash))
            new_file = os.path.join(basedir, hash + ext)
        else:
            log.debug('Not obscuring original image name.')
            new_file = os.path.join(basedir, hash, base + ext)

        # make sure the destination directory exists
        #try:
        #    root = self.get_url_path(new_file)
        #    os.makedirs(os.path.dirname(root))
        #except OSError, exc:
        #    if exc.errno == errno.EEXIST:
        #        # not to worry, directory exists
        #        pass
        #    else:
        #        log.error('Error creating path: %s' % traceback.format_exc())
        #        raise
        #else:
        #    log.debug('Created directory: %s' % root)

        return new_file

    def create_watermark(self, target, mark, path, quality=QUALITY, **kwargs):
        """Create the watermarked image on the filesystem"""

        im = utils.watermark(target, mark, **kwargs)
        #im.save(path, quality=quality)
        path = path.split('static')[-1]
        store_image_to_s3(im, path, quality, 'jpeg')
        return im

def watermark(url, args=''):
    """Returns the URL to a watermarked copy of the image specified."""

    # initialize some variables
    args = args.split(',')
    name = args.pop(0)
    opacity = 0.5
    tile = False
    scale = 1.0
    greyscale = False
    rotation = 0
    position = None
    obscure = OBSCURE
    quality = QUALITY
    random_position_once = RANDOM_POS_ONCE

    # iterate over all parameters to see what we need to do
    for arg in args:
        key, value = arg.split('=')
        key = key.strip()
        value = value.strip()
        if key == 'position':
            position = value
        elif key == 'opacity':
            opacity = utils._percent(value)
        elif key == 'tile':
            tile = bool(int(value))
        elif key == 'scale':
            scale = value
        elif key == 'greyscale':
            greyscale = bool(int(value))
        elif key == 'rotation':
            rotation = value
        elif key == 'obscure':
            obscure = bool(int(value))
        elif key == 'quality':
            quality = int(value)
        elif key == 'random_position_once':
            random_position_once = bool(int(value))

    mark = Watermarker()
    return mark(url, name, position, opacity, tile, scale, greyscale,
                  rotation, obscure, quality, random_position_once)

register.filter(watermark)
