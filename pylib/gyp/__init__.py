#!/usr/bin/python

import optparse
import os.path
import re
import sys


def BuildFileAndTarget(build_file, target):
  # NOTE: If you just want to split up target into a build_file and target,
  # and you know that target already has a build_file that's been produced by
  # this function, pass '' for build_file.

  target_split = target.split(':', 1)
  if len(target_split) == 2:
    [build_file_rel, target] = target_split

    # If a relative path, build_file_rel is relative to the directory
    # containing build_file.  If build_file is not in the current directory,
    # build_file_rel is not a usable path as-is.  Resolve it by interpreting it
    # as relative to build_file.  If build_file_rel is absolute, it is usable
    # as a path regardless of the current directory, and os.path.join will
    # return it as-is.
    build_file = os.path.normpath(os.path.join(os.path.dirname(build_file),
                                               build_file_rel))

  return [build_file, target, build_file + ':' + target]


def QualifiedTarget(build_file, target):
  # "Qualified" means the file that a target was defined in and the target
  # name, separated by a colon.
  return BuildFileAndTarget(build_file, target)[2]


def ExceptionAppend(e, msg):
  if not e.args:
    e.args = [msg]
  elif len(e.args) == 1:
    e.args = [str(e.args[0]) + ' ' + msg]
  else:
    e.args = [str(e.args[0]) + ' ' + msg, e.args[1:]]


def LoadOneBuildFile(build_file_path, variables):
  build_file = open(build_file_path)
  build_file_contents = build_file.read()
  build_file.close()

  build_file_data = None
  try:
    build_file_data = eval(build_file_contents, {'__builtins__': None}, None)
  except SyntaxError, e:
    e.filename = build_file_path
    raise
  except Exception, e:
    ExceptionAppend(e, 'while reading ' + build_file_path)
    raise

  # TODO(mark): First, do includes.  Then, merge in target_defaults sections.
  # Then, do the "pre"/"early" variable expansions and condition evaluations.

  # Apply "pre"/"early" variable expansions and condition evaluations.
  ProcessVariablesAndConditionsInDict(build_file_data, False, variables)

  # Scan for includes and merge them in.
  try:
    LoadBuildFileIncludesIntoDict(build_file_data, build_file_path, variables)
  except Exception, e:
    ExceptionAppend(e, 'while reading includes of ' + build_file_path)
    raise

  return build_file_data


def LoadBuildFileIncludesIntoDict(subdict, subdict_path, variables):
  if 'includes' in subdict:
    # Unhook the includes list, it's no longer needed.
    includes_list = subdict['includes']
    del subdict['includes']

    # Replace it by merging in the included files.
    for include in includes_list:
      MergeDicts(subdict, LoadOneBuildFile(include, variables), subdict_path,
                 include)

  # Recurse into subdictionaries.
  for k, v in subdict.iteritems():
    if v.__class__ == dict:
      LoadBuildFileIncludesIntoDict(v, subdict_path, variables)
    elif v.__class__ == list:
      LoadBuildFileIncludesIntoList(v, subdict_path, variables)


# This recurses into lists so that it can look for dicts.
def LoadBuildFileIncludesIntoList(sublist, sublist_path, variables):
  for item in sublist:
    if item.__class__ == dict:
      LoadBuildFileIncludesIntoDict(item, sublist_path, variables)
    elif item.__class__ == list:
      LoadBuildFileIncludesIntoList(item, sublist_path, variables)


# TODO(mark): I don't love this name.  It just means that it's going to load
# a build file that contains targets and is expected to provide a targets dict
# that contains the targets...
def LoadTargetBuildFile(build_file_path, data, variables):
  if build_file_path in data:
    # Already loaded.
    return

  build_file_data = LoadOneBuildFile(build_file_path, variables)
  data[build_file_path] = build_file_data

  # ...it's loaded and it should have EARLY references and conditionals
  # all resolved and includes merged in...at least it will eventually...

  # Look for dependencies.  This means that dependency resolution occurs
  # after "pre" conditionals and variable expansion, but before "post" -
  # in other words, you can't put a "dependencies" section inside a "post"
  # conditional within a target.

  if 'targets' in build_file_data:
    for target_dict in build_file_data['targets']:
      if 'dependencies' not in target_dict:
        continue
      for dependency in target_dict['dependencies']:
        other_build_file = BuildFileAndTarget(build_file_path, dependency)[0]
        LoadTargetBuildFile(other_build_file, data, variables)

  return data


early_variable_re = re.compile('(<\((.*?)\))')
late_variable_re = re.compile('(>\((.*?)\))')

def ExpandVariables(input, is_late, variables):
  # Look for the pattern that gets expanded into variables
  if not is_late:
    variable_re = early_variable_re
  else:
    variable_re = late_variable_re
  matches = variable_re.findall(input)

  output = input
  if matches != None:
    # Reverse the list of matches so that replacements are done right-to-left.
    # That ensures that earlier re.sub calls won't mess up the string in a
    # way that causes later calls to find the earlier substituted text instead
    # of what's intended for replacement.
    matches.reverse()
    for match in matches:
      # match[0] is the substring to look for and match[1] is the name of
      # the variable.
      if not match[1] in variables:
        raise KeyError, 'Undefined variable ' + match[1] + ' in ' + input
      output = re.sub(re.escape(match[0]), variables[match[1]], output)
  return output


def ProcessConditionsInDict(the_dict, is_late, variables):
  # If the_dict has a "conditions" key (is_late == False) or a
  # "target_conditons" key (is_late == True), its value is treated as a list.
  # Each item in the list consists of cond_expr, a string expression evaluated
  # as the condition, and true_dict, a dict that will be merged into the_dict
  # if cond_expr evaluates to true.  Optionally, a third item, false_dict, may
  # be present.  false_dict is merged into the_dict if cond_expr evaluates to
  # false.  This function will recurse into true_dict or false_dict as
  # appropriate before merging it into the_dict, allowing for nested
  # conditions.

  if not is_late:
    conditions_key = 'conditions'
  else:
    conditions_key = 'target_conditions'

  if not conditions_key in the_dict:
    return

  conditions_list = the_dict[conditions_key]
  # Unhook the conditions list, it's no longer needed.
  del the_dict[conditions_key]

  for condition in conditions_list:
    if not isinstance(condition, list):
      raise TypeError, conditions_key + ' must be a list'
    if len(condition) != 2 and len(condition) != 3:
      # It's possible that condition[0] won't work in which case this won't
      # attempt will raise its own IndexError.  That's probably fine.
      raise IndexError, conditions_key + ' ' + condition[0] + \
                        ' must be length 2 or 3, not ' + len(condition)

    [cond_expr, true_dict] = condition[0:2]
    false_dict = None
    if len(condition) == 3:
      false_dict = condition[2]

    # TODO(mark): Catch exceptions here to re-raise after providing a little
    # more error context.  The name of the file being processed and the
    # condition in quesiton might be nice.
    if eval(cond_expr, {'__builtins__': None}, variables):
      merge_dict = true_dict
    else:
      merge_dict = false_dict

    if merge_dict != None:
      # Recurse to pick up nested conditions.
      ProcessConditionsInDict(merge_dict, is_late, variables)

      # For now, it's OK to pass '', '' for the build files because everything
      # comes from the same build file and everything is already relative to
      # the same place.  If the path to the build file being processed were to
      # be available, which might be nice for error reporting, it should be
      # passed in these two arguments.
      MergeDicts(the_dict, merge_dict, '', '')


def LoadAutomaticVariablesFromDict(variables, the_dict):
  # Any keys with plain string values in the_dict become automatic variables.
  # The variable name is the key name with a "_" character prepended.
  for key, value in the_dict.iteritems():
    if isinstance(value, str) or isinstance(value, int):
      variables['_' + key] = value


def LoadVariablesFromVariablesDict(variables, the_dict):
  # Any keys in the_dict's "variables" dict, if it has one, becomes a
  # variable.  The variable name is the key name in the "variables" dict.
  if 'variables' in the_dict:
    variables.update(the_dict['variables'])


def ProcessVariablesAndConditionsInDict(the_dict, is_late, variables):
  """Handle all variable expansion and conditional evaluation.

  This function is the public entry point for all variable expansions and
  conditional evaluations.
  """

  # Save a copy of the variables dict before loading automatics or the
  # variables dict.  After performing steps that may result in either of
  # these changing, the variables can be reloaded from the copy.
  variables_copy = variables.copy()
  LoadAutomaticVariablesFromDict(variables, the_dict)

  if 'variables' in the_dict:
    # Handle the associated variables dict first, so that any variable
    # references within can be resolved prior to using them as variables.
    # Pass a copy of the variables dict to avoid having it be tainted.
    # Otherwise, it would have extra automatics added for everything that
    # should just be an ordinary variable in this scope.
    ProcessVariablesAndConditionsInDict(the_dict['variables'], is_late,
                                        variables.copy())

  LoadVariablesFromVariablesDict(variables, the_dict)

  for key, value in the_dict.iteritems():
    # Skip "variables", which was already processed if present.
    if key != 'variables' and isinstance(value, str):
      the_dict[key] = ExpandVariables(value, is_late, variables)

  # Variable expansion may have resulted in changes to automatics.  Reload.
  # TODO(mark): Optimization: only reload if no changes were made.
  variables = variables.copy()
  LoadAutomaticVariablesFromDict(variables, the_dict)
  LoadVariablesFromVariablesDict(variables, the_dict)

  # Process conditions in this dict.  This is done after variable expansion
  # so that conditions may take advantage of expanded variables.  For example,
  # if the_dict contains:
  #   {'type':       '<(library_type)',
  #    'conditions': [['_type=="static_library"', { ... }]]}, 
  # _type, as used in the condition, will only be set to the value of
  # library_type if variable expansion is performed before condition
  # processing.  However, condition processing should occur prior to recursion
  # so that variables (both automatic and "variables" dict type) may be
  # adjusted by conditions sections, merged into the_dict, and have the
  # intended impact on contained dicts.
  #
  # This arrangement means that a "conditions" section containing a "variables"
  # section will only have those variables effective in subdicts, not in
  # the_dict.  The workaround is to put a "conditions" section within a
  # "variables" section.  For example:
  #   {'conditions': [['os=="mac"', {'variables': {'define': 'IS_MAC'}}]],
  #    'defines':    ['<(define)'],
  #    'my_subdict': {'defines': ['<(define)']}},
  # will not result in "IS_MAC" being appended to the "defines" list in the
  # current scope but would result in it being appended to the "defines" list
  # within "my_subdict".  By comparison:
  #   {'variables': {'conditions': [['os=="mac"', {'define': 'IS_MAC'}]]},
  #    'defines':    ['<(define)'],
  #    'my_subdict': {'defines': ['<(define)']}},
  # will append "IS_MAC" to both "defines" lists.

  ProcessConditionsInDict(the_dict, is_late, variables)

  # Conditional processing may have resulted in changes to automatics or the
  # variables dict.  Reload.
  # TODO(mark): Optimization: only reload if no changes were made.
  # ProcessConditonsInDict could return a value indicating whether changes
  # were made.
  variables = variables.copy()
  LoadAutomaticVariablesFromDict(variables, the_dict)
  LoadVariablesFromVariablesDict(variables, the_dict)

  # Recurse into child dicts, or process child lists which may result in
  # further recursion into descendant dicts.
  for key, value in the_dict.iteritems():
    # Skip "variables" and string values, which were already processed if
    # present.
    if key == 'variables' or isinstance(value, str):
      continue
    if isinstance(value, dict):
      # Pass a copy of the variables dict so that subdicts can't influence
      # parents.
      ProcessVariablesAndConditionsInDict(value, is_late, variables.copy())
    elif isinstance(value, list):
      # The list itself can't influence the variables dict, and
      # ProcessVariablesAndConditionsInList will make copies of the variables
      # dict if it needs to pass it to something that can influence it.  No
      # copy is necessary here.
      ProcessVariablesAndConditionsInList(value, is_late, variables)
    elif not isinstance(value, int):
      raise TypeError, 'Unknown type ' + value.__class__.__name__ + \
                       ' for ' + key


def ProcessVariablesAndConditionsInList(the_list, is_late, variables):
  # Iterate using an index so that new values can be assigned into the_list.
  index = 0
  while index < len(the_list):
    item = the_list[index]
    if isinstance(item, dict):
      # Make a copy of the variables dict so that it won't influence anything
      # outside of its own scope.
      ProcessVariablesAndConditionsInDict(item, is_late, variables.copy())
    elif isinstance(item, list):
      ProcessVariablesAndConditionsInList(item, is_late, variables)
    elif isinstance(item, str):
      the_list[index] = ExpandVariables(item, is_late, variables)
    elif not isinstance(item, int):
      raise TypeError, 'Unknown type ' + item.__class__.__name__ + \
                       ' at index ' + index
    index = index + 1


class DependencyGraphNode(object):
  """

  Class variables:
    linkable_types: A list of types that are treated as linkable.

  Attributes:
    ref: A reference to an object that this DependencyGraphNode represents.
    dependencies: List of DependencyGraphNodes on which this one depends.
    dependents: List of DependencyGraphNodes that depend on this one.
  """

  linkable_types = ['executable', 'shared_library']

  class CircularException(Exception):
    pass

  def __init__(self, ref):
    self.ref = ref
    self.dependencies = []
    self.dependents = []

  def FlattenToList(self):
    # flat_list is the sorted list of dependencies - actually, the list items
    # are the "ref" attributes of DependencyGraphNodes.  Every target will
    # appear in flat_list after all of its dependencies, and before all of its
    # dependents.
    flat_list = []

    # in_degree_zeros is the list of DependencyGraphNodes that have no
    # dependencies not in flat_list.  Initially, it is a copy of the children
    # of this node, because when the graph was built, nodes with no
    # dependencies were made implicit dependents of the root node.
    in_degree_zeros = self.dependents[:]

    while in_degree_zeros:
      # Nodes in in_degree_zeros have no dependencies not in flat_list, so they
      # can be appended to flat_list.  Take these nodes out of in_degree_zeros
      # as work progresses, so that the next node to process from the list can
      # always be accessed at a consistent position.
      node = in_degree_zeros.pop(0)
      flat_list.append(node.ref)

      # Look at dependents of the node just added to flat_list.  Some of them
      # may now belong in in_degree_zeros.
      for node_dependent in node.dependents:
        is_in_degree_zero = True
        for node_dependent_dependency in node_dependent.dependencies:
          if not node_dependent_dependency.ref in flat_list:
            # The dependent one or more dependencies not in flat_list.  There
            # will be more chances to add it to flat_list when examining
            # it again as a dependent of those other dependencies, provided
            # that there are no cycles.
            is_in_degree_zero = False
            break

        if is_in_degree_zero:
          # All of the dependent's dependencies are already in flat_list.  Add
          # it to in_degree_zeros where it will be processed in a future
          # iteration of the outer loop.
          in_degree_zeros.append(node_dependent)

    return flat_list

  def DirectDependents(self):
    """Returns a list of just direct dependents."""
    dependents = []
    for dependent in self.dependents:
      if dependent.ref not in dependents:
        dependents.append(dependent.ref)

    return dependents

  def DeepDependents(self, dependents=None):
    """Returns a list of all of a target's dependents, recursively."""
    if dependents == None:
      dependents = []

    for dependent in self.dependents:
      if dependent.ref not in dependents:
        # Put each dependent as well as its dependents into the list.
        dependents.append(dependent.ref)
        dependent.DeepDependents(dependents)

    return dependents

  def LinkDependents(self, targets, dependents=None):
    """Returns a list of dependent targets, or self, that are linked.

    Not all target types are linked, where "link" means output by ld or
    link.exe, which link against other libraries and perform undefined symbol
    resolution.  Static library targets are an example of a non-linked target
    type.  This function returns the list of targets in which this target will
    itself be linked.

    If this target is itself a linkable type, the returned list will only
    contain one entry, for this target.

    If this target is not a linkable type, LinkDependents will recurse into
    dependents to determine the nearest linkable dependent targets, and return
    them.
    """
    if dependents == None:
      dependents = []

    # It's kind of sucky that |targets| has to be passed into this function,
    # but that's presently the easiest way to access the target dicts so that
    # this function can find target types.
    target_type = targets[self.ref]['type']
    if target_type in self.linkable_types:
      if self.ref not in dependents:
        dependents.append(self.ref)
    else:
      for dependent in self.dependents:
        dependent.LinkDependents(targets, dependents)

    return dependents

  def DirectDependencies(self, dependencies=None):
    """Returns a list of just direct dependencies."""
    if dependencies == None:
      dependencies = []

    for dependency in self.dependencies:
      # Check for None, corresponding to the root node.
      if dependency.ref != None and dependency.ref not in dependencies:
        dependencies.append(dependency.ref)

    return dependencies

  def DeepDependencies(self, dependencies=None):
    """Returns a list of all of a target's dependencies, recursively."""
    if dependencies == None:
      dependencies = []

    for dependency in self.dependencies:
      # Check for None, corresponding to the root node.
      if dependency.ref != None and dependency.ref not in dependencies:
        dependencies.append(dependency.ref)
        dependency.DeepDependencies(dependencies)

    return dependencies

  def LinkDependencies(self, targets, dependencies=None, initial=True):
    """Returns a list of dependency targets that are linked into this target.

    This function has a split personality, depending on the setting of
    |initial|.  Outside calers should always leave |initial| at its default
    setting.

    When |initial| is True, if |self| is a linkable target, it will be added
    to the list of dependencies, and if |self| is not a linkable target, an
    empty list of dependencies will be returned, because |self| cannot be a
    linkable dependency of itself as a non-linkable type.

    When |initial| is False, the opposite occurs.  If |self| is not linkable,
    it will be added to the list of dependencies, because it will be linked
    when built into the target for which the dependencies list is being built.
    If |self| is linkable, it is not added to the list of dependencies, because
    it is itself linkable, and it will not be linked into the target for which
    the list is being built.

    When adding a target to the list of dependencies, this function will
    recurse into itself with |initial| set to False, to collect depenedencies
    that are linked into the linkable target for which the list is being built.
    """
    if dependencies == None:
      dependencies = []

    # Check for None, corresponding to the root node.
    if self.ref == None:
      return dependencies

    # It's kind of sucky that |targets| has to be passed into this function,
    # but that's presently the easiest way to access the target dicts so that
    # this function can find target types.

    is_linkable = targets[self.ref]['type'] in self.linkable_types

    if (initial and not is_linkable) or (not initial and is_linkable):
      # If this is the first target being examined and it's not linkable,
      # return an empty list of link dependencies, because the link
      # dependencies are intended to apply to the target itself (initial is
      # True) and this target won't be linked.
      # If this is a subsequent target and it's linkable, bail out leaving
      # |dependencies| untouched.  The subsequent target is itself a linkable,
      # it does not get linked into the target for which the dependencies list
      # is being built (although that target links against the subsequent
      # target).
      return dependencies

    # Either (not initial and not is_linkable) or (initial and is_linkable)
    # is true here.  Either way, the target itself will be linked into the
    # target for which the dependencies list is being built.  Add it to the
    # list of dependencies and then recurse.
    if self.ref not in dependencies:
      dependencies.append(self.ref)
      for dependency in self.dependencies:
        dependency.LinkDependencies(targets, dependencies, False)

    return dependencies


def BuildDependencyList(targets):
  # Create a DependencyGraphNode for each target.  Put it into a dict for easy
  # access.
  dependency_nodes = {}
  for target, spec in targets.iteritems():
    if not target in dependency_nodes:
      dependency_nodes[target] = DependencyGraphNode(target)

  # Set up the dependency links.  Targets that have no dependencies are treated
  # as dependent on root_node.
  root_node = DependencyGraphNode(None)
  for target, spec in targets.iteritems():
    target_node = dependency_nodes[target]
    if not 'dependencies' in spec or len(spec['dependencies']) == 0:
      target_node.dependencies = [root_node]
      root_node.dependents.append(target_node)
    else:
      for index in range(0, len(spec['dependencies'])):
        dependency = spec['dependencies'][index]
        target_build_file = BuildFileAndTarget('', target)[0]
        dependency = QualifiedTarget(target_build_file, dependency)
        # Store the qualified name of the target even if it wasn't originally
        # qualified in the dict.  Others will find this useful as well.
        spec['dependencies'][index] = dependency
        dependency_node = dependency_nodes[dependency]
        target_node.dependencies.append(dependency_node)
        dependency_node.dependents.append(target_node)

  # Take the root node out of the list because it doesn't correspond to a real
  # target.
  flat_list = root_node.FlattenToList()

  # If there's anything left unvisited, there must be a circular dependency
  # (cycle).  If you need to figure out what's wrong, look for elements of
  # targets that are not in flat_list.
  if len(flat_list) != len(targets):
    raise DependencyGraphNode.CircularException, \
        'Some targets not reachable, cycle in dependency graph detected'

  return [dependency_nodes, flat_list]


def DoDependentSettings(key, flat_list, targets, dependency_nodes):
  # key should be one of all_dependent_settings, direct_dependent_settings,
  # or link_settings.

  for target in flat_list:
    target_dict = targets[target]
    build_file = BuildFileAndTarget('', target)[0]

    if key == 'all_dependent_settings':
      dependencies = dependency_nodes[target].DeepDependencies()
    elif key == 'direct_dependent_settings':
      dependencies = dependency_nodes[target].DirectDependencies()
    elif key == 'link_settings':
      dependencies = dependency_nodes[target].LinkDependencies(targets)
    else:
      raise KeyError, "DoDependentSettings doesn't know how to determine " + \
                      'dependencies for ' + key

    for dependency in dependencies:
      dependency_dict = targets[dependency]
      if not key in dependency_dict:
        continue
      dependency_build_file = BuildFileAndTarget('', dependency)[0]
      MergeDicts(target_dict, dependency_dict[key],
                 build_file, dependency_build_file)


def RelativePath(path, relative_to):
  # Assuming both |path| and |relative_to| are relative to the current
  # directory, returns a relative path that identifies path relative to
  # relative_to.

  if os.path.isabs(path) != os.path.isabs(relative_to):
    # If one of the paths is absolute, both need to be absolute.
    path = os.path.abspath(path)
    relative_to = os.path.abspath(relative_to)
  else:
    # If both paths are relative, make sure they're normalized.
    path = os.path.normpath(path)
    relative_to = os.path.normpath(relative_to)

  # Split the paths into components.  As a special case, if either path is
  # the current directory, use an empty list as a split-up path.  This must be
  # done because the code that follows is unprepared to deal with "." meaning
  # "current directory" and it will instead assume that it's a subdirectory,
  # which is wrong.  It's possible to wind up with "." when it's passed to this
  # function, for example, by taking the dirname of a relative path in the
  # current directory.
  if path == os.path.curdir:
    path_split = []
  else:
    path_split = path.split(os.path.sep)

  if relative_to == os.path.curdir:
    relative_to_split = []
  else:
    relative_to_split = relative_to.split(os.path.sep)

  # Determine how much of the prefix the two paths share.
  prefix_len = len(os.path.commonprefix([path_split, relative_to_split]))

  # Put enough ".." components to back up out of relative_to to the common
  # prefix, and then append the part of path_split after the common prefix.
  relative_split = [os.path.pardir] * (len(relative_to_split) - prefix_len) + \
                   path_split[prefix_len:]

  # Turn it back into a string and we're done.
  return os.path.join(*relative_split)


def MergeLists(to, fro, to_file, fro_file, is_paths=False, append=True):
  prepend_index = 0

  for item in fro:
    if isinstance(item, str) or isinstance(item, int):
      # The cheap and easy case.
      if is_paths and to_file != fro_file:
        # If item is a relative path, it's relative to the build file dict that
        # it's coming from.  Fix it up to make it relative to the build file
        # dict that it's going into.
        # TODO(mark): We might want to exclude some things here even if
        # is_paths is true, like things that begin with < or > (variables
        # for us) or $ (variables for the build environment).
        to_item = os.path.normpath(os.path.join(
            RelativePath(os.path.dirname(fro_file), os.path.dirname(to_file)),
            item))
      else:
        to_item = item
    elif isinstance(item, dict):
      # Insert a copy of the dictionary.
      to_item = item.copy()
    elif isinstance(item, list):
      # Insert a copy of the list.
      to_item = item[:]
    else:
      raise TypeError, \
          'Attempt to merge list item of unsupported type ' + \
          item.__class__.__name__

    if append:
      to.append(to_item)
    else:
      # Don't just insert everything at index 0.  That would prepend the new
      # items to the list in reverse order, which would be an unwelcome
      # surprise.
      to.insert(prepend_index, to_item)
      prepend_index = prepend_index + 1


def MergeDicts(to, fro, to_file, fro_file):
  # I wanted to name the parameter "from" but it's a Python keyword...
  for k, v in fro.iteritems():
    # It would be nice to do "if not k in to: to[k] = v" but that wouldn't give
    # copy semantics.  Something else may want to merge from the |fro| dict
    # later, and having the same dict ref pointed to twice in the tree isn't
    # what anyone wants considering that the dicts may subsequently be
    # modified.
    if k in to and v.__class__ != to[k].__class__:
      raise TypeError, \
          'Attempt to merge dict value of type ' + v.__class__.__name__ + \
          ' into incompatible type ' + to[k].__class__.__name__ + \
          ' for key ' + k
    if isinstance(v, str) or isinstance(v, int):
      # Overwrite the existing value, if any.  Cheap and easy.
      to[k] = v
    elif isinstance(v, dict):
      # Recurse, guaranteeing copies will be made of objects that require it.
      if not k in to:
        to[k] = {}
      MergeDicts(to[k], v, to_file, fro_file)
    elif isinstance(v, list):
      # Lists in dicts can be merged with different policies, depending on
      # how the key in the "from" dict (k, the from-key) is written.
      #
      # If the from-key has          ...the to-list will have this action
      # this character appended:...     applied when receiving the from-list:
      #                           =  replace
      #                           +  prepend
      #                           ?  set, only if to-list does not yet exist
      #                      (none)  append
      #
      # This logic is list-specific, but since it relies on the associated
      # dict key, it's checked in this dict-oriented function.
      ext = k[-1]
      append = True
      if ext == '=':
        list_base = k[:-1]
        lists_incompatible = [list_base, list_base + '?']
        to[list_base] = []
      elif ext == '+':
        list_base = k[:-1]
        lists_incompatible = [list_base + '=', list_base + '?']
        append = False
      elif ext == '?':
        list_base = k[:-1]
        lists_incompatible = [list_base, list_base + '=', list_base + '+']
      else:
        list_base = k
        lists_incompatible = [list_base + '=', list_base + '?']

      # Some combinations of merge policies appearing together are meaningless.
      # It's stupid to replace and append simultaneously, for example.  Append
      # and prepend are the only policies that can coexist.
      for list_incompatible in lists_incompatible:
        if list_incompatible in fro:
          raise KeyError, 'Incompatible list policies ' + k + ' and ' + \
                          list_incompatible

      if list_base in to:
        if ext == '?':
          # If the key ends in "?", the list will only be merged if it doesn't
          # already exist.
          continue
        if not isinstance(to[list_base], list):
          # This may not have been checked above if merging in a list with an
          # extension character.
          raise TypeError, \
              'Attempt to merge dict value of type ' + v.__class__.__name__ + \
              ' into incompatible type ' + to[list_base].__class__.__name__ + \
              ' for key ' + list_base + '(' + k + ')'
      else:
        to[list_base] = []

      # Call MergeLists, which will make copies of objects that require it.
      is_paths = list_base in ['include_dirs', 'sources',
                               'xcode_framework_dirs']
      MergeLists(to[list_base], v, to_file, fro_file, is_paths, append)
    else:
      raise TypeError, \
          'Attempt to merge dict value of unsupported type ' + \
          v.__class__.__name__ + ' for key ' + k


def ProcessRules(name, the_dict):
  """Process regular expression and exclusion-based rules on lists.

  An exclusion list is in a dict key named with a trailing "!", like
  "sources!".  Every item in such a list is removed from the associated
  main list, which in this example, would be "sources".  Removed items are
  placed into a "sources_excluded" list in the dict.

  Regular expression (regex) rules are contained in dict keys named with a
  trailing "/", such as "sources/" to operate on the "sources" list.  Regex
  rules in a dict take the form:
    'sources/': [ ['exclude', '_(linux|mac|win)\\.cc$'] ],
                  ['include', '_mac\\.cc$'] ],
  The first rule says to exclude all files ending in _linux.cc, _mac.cc, and
  _win.cc.  The second rule then includes all files ending in _mac.cc that
  are now or were once in the "sources" list.  Items matching an "exclude"
  rule are subject to the same processing as would occur if they were listed
  by name in an exclusion list (ending in "!").  Items matching an "include"
  rule are brought back into the main list if previously excluded by an
  exclusion list or exclusion regex rule, and are protected from future removal
  by such exclusion lists and rules.
  """

  # Look through the dictionary for any lists whose keys end in "!" or "/".
  # These are lists that will be treated as exclude lists and regular
  # expression-based exclude/include lists.  Collect the lists that are
  # needed first, looking for the lists that they operate on, and assemble
  # then into |lists|.  This is done in a separate loop up front, because
  # the _included and _excluded keys need to be added to the_dict, and that
  # can't be done while iterating through it.

  lists = []
  del_lists = []
  for key, value in the_dict.iteritems():
    operation = key[-1]
    if operation != '!' and operation != '/':
      continue

    if not isinstance(value, list):
      raise ValueError, name + ' key ' + key + ' must be list, not ' + \
                        value.__class__.__name__

    list_key = key[:-1]
    if list_key not in the_dict:
      # This happens when there's a list like "sources!" but no corresponding
      # "sources" list.  Since there's nothing for it to operate on, queue up
      # the "sources!" list for deletion now.
      del_lists.append(key)
      continue

    if not isinstance(the_dict[list_key], list):
      raise ValueError, name + ' key ' + list_key + \
                        ' must be list, not ' + \
                        value.__class__.__name__ + ' when applying ' + \
                        {'!': 'exclusion', '/': 'regex'}[operation]

    if not list_key in lists:
      lists.append(list_key)

  # Delete the lists that are known to be unneeded at this point.
  for del_list in del_lists:
    del the_dict[del_list]

  for list_key in lists:
    # Initialize the _excluded list now, so that the code that needs to use
    # it can perform list operations without needing to do its own lazy
    # initialization.  If the list is unneeded, it will be deleted at the end.
    excluded_key = list_key + '_excluded'
    if excluded_key in the_dict:
      raise KeyError, \
          name + ' key ' + excluded_key + ' must not be present prior ' + \
          ' to applying exclusion/regex rules for ' + list_key
    the_dict[excluded_key] = []

    # Also initialize the included_list, which doesn't need to be part of
    # the_dict.
    included_list = []

    # Note that exclude_key ("sources!") is different from excluded_key
    # ("sources_excluded").  Since exclude_key is just a very temporary
    # variable used on the next few lines, this isn't a huge problem, but
    # be careful!
    exclude_key = list_key + '!'
    if exclude_key in the_dict:
      for exclude_item in the_dict[exclude_key]:
        if exclude_item in included_list:
          # The exclude_item was already preserved and is "golden", don't touch
          # it.
          continue

        # The exclude_item may appear in the list more than once, so loop to
        # remove it.  That's "while exclude_item in", not "for exclude_item
        # in."  Crucial difference.
        removed = False
        while exclude_item in the_dict[list_key]:
          removed = True
          the_dict[list_key].remove(exclude_item)

        # If anything was removed, add it to the _excluded list.
        if removed:
          if not exclude_item in the_dict[excluded_key]:
            the_dict[excluded_key].append(exclude_item)

      # The "whatever!" list is no longer needed, dump it.
      del the_dict[exclude_key]

    regex_key = list_key + '/'
    if regex_key in the_dict:
      for regex_item in the_dict[regex_key]:
        [action, pattern] = regex_item
        pattern_re = re.compile(pattern)

        # Instead of writing "for list_item in the_dict[list_key]", write a
        # while loop.  Iteration with a for loop won't work, because code that
        # follows manipulates the_dict[list_key].  Be careful with that "index"
        # variable.
        index = 0
        while index < len(the_dict[list_key]):
          list_item = the_dict[list_key][index]
          if pattern_re.search(list_item):
            # Regular expression match.

            if action == 'exclude':
              if list_item in included_list:
                # regex_item says to remove list_item from the list, but
                # something else already said to include it, so leave it
                # alone and proceed to the next item in the list.
                index = index + 1
                continue

              del the_dict[list_key][index]

              # Add it to the excluded list if it's not already there.
              if not list_item in the_dict[excluded_key]:
                the_dict[excluded_key].append(list_item)

              # continue without incrementing |index|.  The next object to
              # look at, if any, moved into the index of the object that was
              # just removed.
              continue

            elif action == 'include':
              # Here's a list_item that's in list and needs to stay there.
              # Add it to the golden list of happy items to keep, and nothing
              # will be able to exclude it in the future.
              if not list_item in included_list:
                included_list.append(list_item)

            else:
              # This is an action that doesn't make any sense.
              raise ValueError, 'Unrecognized action ' + action + ' in ' + \
                                name + ' key ' + key

          # Advance to the next list item.
          index = index + 1

        if action == 'include':
          # Items matching an include pattern may have already been excluded.
          # Resurrect any that are found.  The while loop is needed again
          # because the excluded list will be manipulated.
          index = 0
          while index < len(the_dict[excluded_key]):
            excluded_item = the_dict[excluded_key][index]
            if pattern_re.search(excluded_item):
              # Yup, this is one.  Take it out of the excluded list and put
              # it back into the main list AND the golden included list, so
              # that nothing else can touch it.  Unfortunately, the best that
              # can be done at this point is an append, since there's no way
              # to know where in the list it came from.  TODO(mark): There
              # are possible solutions to this problem, like tracking
              # include/exclude status in a parallel list and only doing the
              # deletions after processing all of the rules.
              del the_dict[excluded_key][index]
              the_dict[list_key].append(excluded_item)
              if not excluded_item in included_list:
                included_list.append(excluded_item)
            else:
              # Only move to the next index if there was no match.  If there
              # was a match, the item was deleted, and the next item to look
              # at is at the same index as the item just examined.
              index = index + 1

      # The "whatever/" list is no longer needed, dump it.
      del the_dict[regex_key]

    # Dump the "excluded" list if it's empty.
    if len(the_dict[excluded_key]) == 0:
      del the_dict[excluded_key]


def FindBuildFiles():
  extension = '.gyp'
  files = os.listdir(os.getcwd())
  build_files = []
  for file in files:
    if file[-len(extension):] == extension:
      build_files.append(file)
  return build_files


def main(args):
  my_name = os.path.basename(sys.argv[0])

  parser = optparse.OptionParser()
  usage = 'usage: %s [-D var=val ...] [-f format] [build_file ...]'
  parser.set_usage(usage.replace('%s', '%prog'))
  parser.add_option('-D', dest='defines', action='append', metavar='VAR=VAL',
                    help='sets variable VAR to value VAL')
  parser.add_option('-f', '--format', dest='format',
                    help='output format to generate')
  (options, build_files) = parser.parse_args(args)
  if not options.format:
    options.format = {'darwin': 'xcodeproj',
                      'win32':  'msvs',
                      'cygwin': 'msvs'}[sys.platform]
  if not build_files:
    build_files = FindBuildFiles()
  if not build_files:
    print >>sys.stderr, (usage + '\n\n%s: error: no build_file') % \
                        (my_name, my_name)
    return 1

  default_variables = {}

  # -D on the command line sets variable defaults - D isn't just for define,
  # it's for default.  Perhaps there should be a way to force (-F?) a
  # variable's value so that it can't be overridden by anything else.
  if options.defines:
    for define in options.defines:
      tokens = define.split('=', 1)
      if len(tokens) == 2:
        # Set the variable to the supplied value.
        default_variables[tokens[0]] = tokens[1]
      else:
        # No value supplied, treat it as a boolean and set it.
        default_variables[tokens[0]] = True

  # Default variables provided by this program and its modules should be
  # named WITH_CAPITAL_LETTERS to provide a distinct "best practice" namespace,
  # avoiding collisions with user and automatic variables.
  default_variables['GENERATOR'] = options.format

  generator_name = 'gyp.generator.' + options.format
  # These parameters are passed in order (as opposed to by key)
  # because ActivePython cannot handle key parameters to __import__.
  generator = __import__(generator_name, globals(), locals(), generator_name)
  default_variables.update(generator.generator_default_variables)

  # Load build files.  This loads every target-containing build file into
  # the |data| dictionary such that the keys to |data| are build file names,
  # and the values are the entire build file contents after "early" or "pre"
  # processing has been done and includes have been resolved.
  data = {}
  for build_file in build_files:
    LoadTargetBuildFile(build_file, data, default_variables)

  # Build a dict to access each target's subdict by qualified name.
  targets = {}
  for build_file in data:
    if 'targets' in data[build_file]:
      for target in data[build_file]['targets']:
        target_name = QualifiedTarget(build_file, target['name'])
        targets[target_name] = target

  # BuildDependencyList will also fix up all dependency lists to contain only
  # qualified names.  That makes it much easier to see if a target is already
  # in a dependency list, because the name it will be listed by is known.
  # This is used below when the dependency lists are adjusted for static
  # libraries.  The only thing I don't like about this is that it seems like
  # BuildDependencyList shouldn't modify "targets".  I thought we looped over
  # "targets" too many times, though, and that seemed like a good place to do
  # this fix-up.  We may want to revisit where this is done.
  [dependency_nodes, flat_list] = BuildDependencyList(targets)

  # Look at each project's settings dict, and merge settings into targets.
  # TODO(mark): Move this step into LoadOneBuildFile or something similar.
  # This step should happen immediately before or after (it doesn't really
  # matter which) includes are added.  The policy should be for target
  # dicts to inherit from the root settings dict, which means that for the
  # MergeDicts procedure, the target dict should actually be trated as the
  # "fro" dict to be merged into a deep copy of the settings dict, which
  # should be the "to" dict which, after merging, replaces the original target
  # dict.
  for build_file_name, build_file_data in data.iteritems():
    if 'settings' in build_file_data:
      file_settings = build_file_data['settings']
      for target_dict in build_file_data['targets']:
        MergeDicts(target_dict, file_settings, build_file_name, build_file_name)

  # Handle dependent settings of various types.
  for settings_type in ['all_dependent_settings',
                        'direct_dependent_settings',
                        'link_settings']:
    DoDependentSettings(settings_type, flat_list, targets, dependency_nodes)

  # TODO(mark): This logic is rough, but it works for base_unittests.
  # Set up computed dependencies.  For each non-static library target, look
  # at the entire dependency hierarchy and add any static libraries as computed
  # dependencies.  Static library targets have no computed dependencies.  See
  # notes above regarding linkables, this section should be refactored at the
  # same time as the above one.
  for target in flat_list:
    target_dict = targets[target]

    # If we've got a static library here...
    if target_dict['type'] == 'static_library':
      dependents = dependency_nodes[target].DeepDependents()
      # TODO(mark): Probably want dependents to be sorted in the order that
      # they appear in flat_list.

      # Look at every target that depends on it, even indirectly...
      for dependent in dependents:
        [dependent_bf, dependent_unq, dependent_q] = \
            BuildFileAndTarget('', dependent)
        dependent_dict = targets[dependent_q]

        # If the dependent isn't a static library...
        if dependent_dict['type'] != 'static_library':

          # Make it depend on the static library if it doesn't already...
          if not 'dependencies' in dependent_dict:
            dependent_dict['dependencies'] = []
          if not target in dependent_dict['dependencies']:
            dependent_dict['dependencies'].append(target)

          # ...and make it link against the libraries that the static library
          # wants, if it doesn't already...
          # TODO(mark): Eliminate the special-casing of the "libraries"
          # section in favor of allowing "libraries" sections to be enclosed
          # within "link_settings" sections in input files.  The recommended
          # best practice should be for "libraries" to only appear within
          # "link_settings" sections.
          if 'libraries' in target_dict:
            if not 'libraries' in dependent_dict:
              dependent_dict['libraries'] = []
            for library in target_dict['libraries']:
              if not library in dependent_dict['libraries']:
                dependent_dict['libraries'].append(library)

      # The static library doesn't need its dependencies or libraries any more.
      if 'dependencies' in target_dict:
        del target_dict['dependencies']
      if 'libraries' in target_dict:
        del target_dict['libraries']

  # Apply "post"/"late"/"target" variable expansions and condition evaluations.
  for target in flat_list:
    target_dict = targets[target]
    ProcessVariablesAndConditionsInDict(target_dict, True, default_variables)

  # Apply exclude (!) and regex (/) rules.
  for target in flat_list:
    target_dict = targets[target]
    ProcessRules(target, target_dict)

  # TODO(mark): Pass |data| for now because the generator needs a list of
  # build files that came in.  In the future, maybe it should just accept
  # a list, and not the whole data dict.
  # NOTE: flat_list is the flattened dependency graph specifying the order
  # that targets may be built.  Build systems that operate serially or that
  # need to have dependencies defined before dependents reference them should
  # generate targets in the order specified in flat_list.
  generator.GenerateOutput(flat_list, targets, data)
  return 0


if __name__ == '__main__':
  sys.exit(main(sys.argv[1:]))
