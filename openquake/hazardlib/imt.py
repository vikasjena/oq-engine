# -*- coding: utf-8 -*-
# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# Copyright (C) 2012-2018 GEM Foundation
#
# OpenQuake is free software: you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenQuake is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenQuake. If not, see <http://www.gnu.org/licenses/>.

"""
Module :mod:`openquake.hazardlib.imt` defines different intensity measure
types.
"""
import ast


class IMTtuple(tuple):
    period = 0
    damping = 5.0

    @classmethod
    def new(cls, string, params=''):
        """
        >>> IMTtuple.new('PGA')
        PGA()
        >>> IMTtuple.new('PGA()')
        PGA()
        >>> IMTtuple.new('SA(0.10)', 'period, damping')
        SA(0.1)
        >>> IMTtuple.new('SA(0.1, 4)', 'period, damping')
        SA(0.1, 4)
        """
        s = string.strip()
        if s[-1] != ')':
            # no parens, PGA is considered the same as PGA()
            return cls((s,))
        prefix, rest = s.split('(', 1)
        if rest == ')':  # no arguments
            return cls((prefix,))
        tup = ast.literal_eval(rest[:-1] + ',')
        return cls((prefix,) + tup, params)

    def __new__(cls, tup, params=""):
        self = tuple.__new__(cls, tup)
        for param, value in zip(params.split(', '), tup[1:]):
            setattr(self, param, value)
        return self

    @property
    def prefix(self):
        """
        :returns: the prefix of the IMT (i.e. SA for Spectral Acceleration)
        """
        return self[0]

    def is_(self, *imt_factories):
        for factory in imt_factories:
            if self.prefix == factory.__name__:
                return True

    def __repr__(self):
        return '%s(%s)' % (self[0], ', '.join(map(repr, self[1:])))


class _IMTregistry(object):
    def __init__(self):
        self.prefixes = {}

    def add(self, prefix, doc, sig='', defaults=None):
        code = 'def %s(%s): return IMTtuple((%r, %s), "%s")' % (
            prefix, sig, prefix, sig, sig)
        dic = {'IMTtuple': IMTtuple}
        exec(code, dic)
        factory = dic[prefix]
        factory.__doc__ = doc
        factory.__defaults__ = defaults
        factory.params = sig
        self.prefixes[prefix] = factory
        return factory

    def __getattr__(self, prefix):
        if prefix not in self.prefixes:
            raise AttributeError(prefix)
        return self.prefixes[prefix]


imt = _IMTregistry()


def from_string(string):
    prefix = string.split('(', 1)[0]
    return IMTtuple.new(string, getattr(imt, prefix).params)


PGA = imt.add('PGA', "Peak ground acceleration during an earthquake measured "
              "in units of ``g``, times of gravitational acceleration.")

PGV = imt.add('PGV', "Peak ground velocity during an earthquake "
              "measured in units of ``cm/sec``.")

PGD = imt.add('PGD', "Peak ground displacement during an earthquake "
              "measured in units of ``cm``.")

SA = imt.add('SA', """\
Spectral acceleration, defined as the maximum acceleration of a damped,
single-degree-of-freedom harmonic oscillator. Units are ``g``, times
of gravitational acceleration.

:param period:
    The natural period of the oscillator in seconds.
""", 'period, damping', defaults=(5,))


IA = imt.add('IA', """\
Arias intensity. Determines the intensity of shaking by measuring
the acceleration of transient seismic waves. Units are ``m/s``.
""")


CAV = imt.add('CAV', """\
Cumulative Absolute Velocity. Defins the integral of the absolute
acceleration time series. Units are "g-sec"
""")


RSD = imt.add('RSD', """\
Relative significant duration, 5-95% of Arias intensity <IA>, in seconds.
""")


RSD595 = imt.add('RSD595', "Alias for RSD")


RSD575 = imt.add('RSD575', """\
Relative significant duration, 5-75% of Arias intensity <IA>, in seconds.
""")


RSD2080 = imt.add('RSD2080', """\
Relative significant duration, 20-80% of Arias intensity <IA>, in seconds.
""")


MMI = imt.add('MMI', """\
Modified Mercalli intensity, a Roman numeral describing the severity
of an earthquake in terms of its effects on the earth's surface
and on humans and their structures.
""")

# ######################### geotechnical IMTs ############################ #

PGDfLatSpread = imt.add('PGDfLatSpread', """\
Permanent ground defomation (m) from lateral spread
""")


PGDfSettle = imt.add('PGDfSettle', """"\
Permanent ground defomation (m) from settlement
""")

PGDfSlope = imt.add('PGDfSlope', """\
Permanent ground deformation (m) from slope failure
""")

PGDfRupture = imt.add('PGDfRupture', """\
Permanent ground deformation (m) from co-seismic rupture
""")
