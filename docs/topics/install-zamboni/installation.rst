.. _installation:

==================
Installing Zamboni
==================

We're going to use all the hottest tools to set up a nice environment.  Skip
steps at your own peril. Here we go!

.. note::

    For less manual work, you can build Zamboni in a
    :doc:`virtual machine using vagrant <install-with-vagrant>`
    but that has known bugs at the time of this writing.
    For best results, install manually.


Need help?
~~~~~~~~~~

Come talk to us on irc://irc.mozilla.org/marketplace if you have questions,
issues, or compliments.


.. _ubuntu-packages:

1. Installing dependencies
--------------------------

On OS X
~~~~~~~
The best solution for installing UNIX tools on OS X is Homebrew_.

The following packages will get you set for zamboni::

    brew install python libxml2 mysql openssl swig jpeg pngcrush redis

On Ubuntu
~~~~~~~~~
The following command will install the required development files on Ubuntu or,
if you're running a recent version, you can `install them automatically
<apt:python-dev,python-virtualenv,libxml2-dev,libxslt1-dev,libmysqlclient-dev,libmemcached-dev,libssl-dev,swig openssl,curl,pngcrush,redis-server>`_::

    sudo aptitude install python-dev python-virtualenv libxml2-dev libxslt1-dev libmysqlclient-dev libssl-dev swig openssl curl pngcrush redis-server

Services
~~~~~~~~
Zamboni has three dependencies you must install and have running:

* MySQL should require no configuration.
* Redis should require no configuration.
* Seee :ref:`elasticsearch <Elasticsearch>` for setup and configuration.

2. Grab the source
------------------

Grab zamboni from github with::

    git clone --recursive git://github.com/mozilla/zamboni.git
    cd zamboni

``zamboni.git`` is all the source code.  :ref:`updating` is detailed later on.

If at any point you realize you forgot to clone with the recursive
flag, you can fix that by running::

    git submodule update --init --recursive


3. Setup a virtualenv
---------------------

`virtualenv`_ is a tool to create
isolated Python environments. This will let you put all of Zamboni's
dependencies in a single directory rather than your global Python directory.
For ultimate convenience, we'll also use `virtualenvwrapper`_
which adds commands to your shell.

Since each shell setup is different, you can install everything you need
and configure your shell using the `virtualenv-burrito`_. Type this::

    curl -s https://raw.github.com/brainsik/virtualenv-burrito/master/virtualenv-burrito.sh | $SHELL

Open a new shell to test it out. You should have the ``workon`` and
``mkvirtualenv`` commands.

.. _Homebrew: http://brew.sh/
.. _virtualenv: http://pypi.python.org/pypi/virtualenv
.. _`virtualenv-burrito`: https://github.com/brainsik/virtualenv-burrito
.. _virtualenvwrapper: http://www.doughellmann.com/docs/virtualenvwrapper/

4. Getting Packages
-------------------

Now we're ready to go, so create an environment for zamboni::

    mkvirtualenv --python=python2.6 zamboni

That creates a clean environment named zamboni using Python 2.6. You can get
out of the environment by restarting your shell or calling ``deactivate``.

To get back into the zamboni environment later, type::

    workon zamboni  # requires virtualenvwrapper

.. note:: Zamboni requires at least Python 2.6.1, production is using
          Python 2.6.6. Python 2.7 is not supported.

.. note:: If you want to use a different Python binary, pass the name (if it is
          on your path) or the full path to mkvirtualenv with ``--python``::

            mkvirtualenv --python=/usr/local/bin/python2.6 zamboni

.. note:: If you are using an older version of virtualenv that defaults to
          using system packages you might need to pass ``--no-site-packages``::

            mkvirtualenv --python=python2.6 --no-site-packages zamboni

First make sure you have a recent `pip`_ for security reasons.
From inside your activated virtualenv, install the required python packages::

    make update_deps

Issues at this point? See :doc:`./troubleshooting`.

5. Settings
-----------

Most of zamboni is already configured in ``mkt.settings.py``, but there's one thing
you'll need to configure locally, the database. The easiest way to do that
is by setting an environment variable (see next section).

Optionally you can create a local settings file and place anything custom
into ``settings_local.py``.

Any file that looks like ``settings_local*`` is for local use only; it will be
ignored by git.

Environment settings
~~~~~~~~~~~~~~~~~~~~

Out of the box, zamboni should work without any need for settings changes.
Some settings are configurable from the environment. See the
`marketplace docs`_ for information on the environment variables and how
they affect zamboni.

6. Setting up a Mysql Database
------------------------------

Instead of running ``manage.py syncdb`` your best bet is to grab a snapshot of
our production DB which has been redacted and pruned for development use.
Development snapshots are hosted over at
https://landfill-mkt.allizom.org/db/.

There is a management command that download and install the landfill
database. You have to create the database first using the following
command filling in the database name from your ``settings_local.py``
(Defaults to ``zamboni``)::

    mysqladmin -uroot create $DB_NAME

Then you can just run the following command to install the landfill
database. You can also use it whenever you want to restore back to the
base landfill database::

    ./manage.py install_landfill

Here are the shell commands to pull down and set up the latest
snapshot manually (ie without the management command)::

Issues at this point? See :doc:`./troubleshooting`.

Database Migrations
~~~~~~~~~~~~~~~~~~

Each incremental change we add to the database is done with a versioned SQL
(and sometimes Python) file. To keep your local DB fresh and up to date, run
migrations like this::

    make update_db

More info on schematic: https://github.com/mozilla/schematic


7. Check it works
-----------------

If you've gotten the system requirements, downloaded ``zamboni`` and
``zamboni-lib``, set up your virtualenv with the compiled packages, and
configured your settings and database, you're good to go::

    ./manage.py runserver

Hit::

    http://localhost:2600/services/monitor

This will report any errors or issues in your installation.

8. Create an admin user
-----------------------

Chances are that for development, you'll want an admin account.

After logging in, run this management command::

    ./manage.py addusertogroup <your email> 1

9. Setting up the consumer pages
--------------------------------

You will not have any consumer pages you will need to install
`Marketplace consumer`_ pages.

Post installation
-----------------

To keep your zamboni up to date with the latest changes in source files,
requrirements and database migrations run::

    make full_update

Advanced Installation
---------------------

In production we use things like memcached, rabbitmq + celery and Stylus.
Learn more about installing these on the :doc:`./advanced-installation` page.

.. note::

    Although we make an effort to keep advanced items as optional installs
    you might need to install some components in order to run tests or start
    up the development server.

.. _`Marketplace consumer`: http://marketplace.readthedocs.org/en/latest/topics/consumer.html
.. _`marketplace docs`: http://marketplace.readthedocs.org/en/latest/topics/setup.html
