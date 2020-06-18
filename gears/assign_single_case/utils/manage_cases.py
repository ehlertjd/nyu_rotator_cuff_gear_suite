import logging
import os

import flywheel
import numpy as np
import pandas as pd

from .container_operations import create_project, export_session, find_or_create_group

log = logging.getLogger(__name__)


class InvalidGroupError(Exception):
    """
    Exception raised for using an Invalid Flywheel group for this gear.

    Args:
        message (str): explanation of the error
    """

    def __init__(self, message):
        Exception.__init__(self)
        self.message = message


class InvalidInputError(Exception):
    """
    Exception raised for using an Invalid Input for this gear.

    Args:
        message (str): explanation of the error
    """

    def __init__(self, message):
        Exception.__init__(self)
        self.message = message


class NoReaderProjectsError(Exception):
    """
    Exception raised when no reader projects exist to populate.

    Args:
        message (str): explanation of the error
    """

    def __init__(self, message):
        Exception.__init__(self)
        self.message = message


class InvalidLaunchContainerError(Exception):
    """
    Exception raised when gear is launched from an invalid container.

    Args:
        message (str): explanation of the error
    """

    def __init__(self, message):
        Exception.__init__(self)
        self.message = message


class InvalidReaderError(Exception):
    """
    Exception raised for referencing an invalid reader of a project.

    Args:
        message (str): explanation of the error
    """

    def __init__(self, message):
        Exception.__init__(self)
        self.message = message


def set_session_features(session, case_coverage):
    """
    Gets or sets session features removes from source, restore later

    Each session has a set of features: case_coverage, assignments, and assignment_count
    each assignment consists of {project_id:<uid>, session_id:<uid>, status:<str>}
    Once diagnosed and measured each assignment will have the additional tags of
    {measurements:{}, read: {}} that are produced as part of the measurement process.
    if not found, create with defaults

    Args:
        session (flywheel.Session): The session to set/retrieve features from
    """

    session_features = (
        session.info["session_features"]
        if session.info.get("session_features")
        else {"case_coverage": case_coverage, "assignments": [], "assigned_count": 0}
    )

    # If the session has features, remove them for now.  Restore them after export
    if session.info.get("session_features"):
        session.delete_info("session_features")

    return session_features


def set_project_session_attributes(session_features):
    """
    Return session attributes generated by assigning sessions to reader projects

    Args:
        session_features (dict): The session features (how many assignments) of the 
            above session

    Returns:
        dict: The compiled attributes of a session for recording at the project level
    """
    session_attributes = {
        "id": session_features["id"],
        "label": session_features["label"],
        "case_coverage": session_features["case_coverage"],
        "unassigned": session_features["case_coverage"]
        - len(session_features["assignments"]),
        "assigned": len(session_features["assignments"]),
        "diagnosed": len(
            [
                assignment["status"]
                for assignment in session_features["assignments"]
                if assignment["status"] == "Diagnosed"
            ]
        ),
        "measured": len(
            [
                assignment["status"]
                for assignment in session_features["assignments"]
                if assignment["status"] == "Measured"
            ]
        ),
        "completed": len(
            [
                assignment["status"]
                for assignment in session_features["assignments"]
                if assignment["status"] == "Completed"
            ]
        ),
    }

    return session_attributes


def update_reader_projects_metadata(fw_client, group_projects, readers_df):
    """
    Update reader group projects' metadata according to the csv/dataframe contents

    Contraints are as follows:
    1) if project.max_cases < df.max_cases, project.max_cases = df.max_cases
    2) if project.max_cases > df.max_cases, project.max_cases = min
        (df.max_cases, project.num_assigned_cases)

    Function loops through the DataFrame and applies updates only to those that 
    exist in the DataFrame and as a reader project.

    Args:
        group_projects (flywheel.Group): Flywheel Group object
        readers_df (pandas.DataFrame): Pandas Dataframe containing columns:
            "email", "first_name", "last_name", and "max_cases"
    """

    # Valid roles for readers are "read-write" and "read-only"
    proj_roles = [
        role.id
        for role in fw_client.get_all_roles()
        if role.label in ["read-write", "read-only"]
    ]

    group_reader_ids = [
        [
            perm.id
            for perm in proj.permissions
            if set(perm.role_ids).intersection(proj_roles)
        ][0]
        for proj in group_projects
    ]
    for index in readers_df.index:
        reader_id = readers_df.email[index]
        # if the csv reader_id is not in the current reader projects, skip
        if reader_id not in group_reader_ids:
            continue

        reader_project = [
            proj
            for proj in group_projects
            if reader_id in [perm.id for perm in proj.permissions]
        ][0].reload()

        csv_max_cases = int(readers_df.max_cases[index])
        project_info = reader_project.info
        project_max_cases = (
            project_info["project_features"]["max_cases"]
            if (
                project_info.get("project_features")
                and project_info["project_features"].get("max_cases")
            )
            else 0
        )
        # TODO: If the reader/user has discovered and changed the info.max_cases,
        # then the following will
        # REVERT info.max_cases to whatever it was OR update --
        # depending on the conditionals below.
        # QUESTION: Do we want it this way? ... How else could we work this?
        # if the csv.max_cases is greater, update
        if csv_max_cases > project_max_cases:
            project_info["project_features"]["max_cases"] = csv_max_cases
        # else check the number of assigned sessions... never set max_cases
        # to less than this (* see todo below *)
        # TODO: Check to see if we can "unassign" incomplete cases
        elif csv_max_cases < project_max_cases:
            project_info["project_features"]["max_cases"] = max(
                len(reader_project.sessions()), csv_max_cases
            )
        # update if csv.max_cases and info.max_cases are different
        if csv_max_cases is not project_max_cases:
            reader_project.update_info(project_info)


def check_valid_reader(fw_client, reader_id, group_id):
    """
    Checks for the existing reader project for indicated reader_id.

    Args:
        fw_client (flywheel.Client): Flywheel instance client for api calls.
        reader_id (str): The email of the reader to validate
        group_id (str): The id of the reader group

    Raises:
        InvalidReaderError: If a project is not found assigned to reader, raised to exit

    Returns:
        boolean: Returns `True` if reader has assigned project
    """

    group_projects = fw_client.projects.find(f'group="{group_id}"')

    proj_roles = [
        role.id
        for role in fw_client.get_all_roles()
        if role.label in ["read-write", "read-only"]
    ]

    valid_reader_ids = [
        [
            perm.id
            for perm in proj.permissions
            if set(perm.role_ids).intersection(proj_roles)
        ][0]
        for proj in group_projects
    ]

    if reader_id in valid_reader_ids:
        return True

    raise InvalidReaderError(
        f"The Reader, {reader_id}, has not been instantiated. "
        "Please run `assign-readers` to create a reader project for this user."
    )


def instantiate_new_readers(fw_client, group, group_readers, readers_df):
    """
    Instantiate and grant permissions to new readers found in readers_df

    Args:
        fw_client (flywheel.Client): The Flywheel client
        group (flywheel.Group): The flywheel group that reader projects are created in
        group_readers (list): ids for each reader with ro/rw permission to the group
        readers_df (pandas.DataFrame): DataFrame for reader updates and creation

    Returns:
        list: A list of reader ids (emails) from the csv requiring a new project
    """
    readers_to_instantiate = []
    # All Flywheel users on instance.
    users_ids = [user.id for user in fw_client.users()]

    # check if the new readers need to be added as new FW users
    new_users = readers_df[~readers_df.email.isin(users_ids)]

    for indx in new_users.index:
        new_user = new_users.loc[indx, :]
        fw_user = flywheel.User(
            id=new_user.email,
            email=new_user.email,
            firstname=new_user.first_name,
            lastname=new_user.last_name,
        )
        fw_client.add_user(fw_user)

    # check if the new readers need to be added to the specific reader group
    new_group_readers = readers_df[~readers_df.email.isin(group_readers)]
    for indx in new_group_readers.index:
        new_group_user = new_group_readers.loc[indx, "email"]
        # TODO: The following line can and will change with the v12.0.0 SDK
        user_permission = {"_id": new_group_user, "access": "rw"}
        group.add_permission(user_permission)
        readers_to_instantiate.append(
            (new_group_user, int(new_group_readers.max_cases[indx]))
        )

    # if we added new group permissions update the group_readers
    if new_group_readers.shape[0] > 0:
        group = group.reload()
        # TODO: Group permissions will be changing
        group_readers = [
            perm.id for perm in group.permissions if perm.access in ["rw", "ro"]
        ]

    return readers_to_instantiate


def create_or_update_reader_projects(
    fw_client, group, master_project, max_cases, readers_csv=None
):
    """
    Updates the number and attributes of reader projects to reflect constraints

    These constraints are:
    1) A reader project must exist for every reader(user) with 'ro' or 'rw' permissions
        in thereader group.
    2) A reader project has a maximum number of cases (max_cases) that the reader will
        review
    3) Readers listed in the reader_csv will exist
        a) As a Flywheel user
        b) As a reader with 'ro' or 'rw' permissions in the reader group
        c) As a sole ro/rw user on a reader project
        d) Has a maximum number cases (max_cases) assigned to the reader project
            according to some additional constraints.

    Args:
        fw_client (flywheel.Client): Flywheel Client object instantiated on instance
        group (flywheel.Group): The group ("readers") to update the reader projects for
        master_project (flywheel.Project): The project we are copying sessions, files,
            and metadata from.
        max_cases (int): The maximum number of cases to be assigned to
            flywheel users newly assigned to the group.
        readers_csv (str, optional): A filepath to the CSV input containing
            reader emails, names, and max_cases for assignment or updating.
                Defaults to None.

    Returns:
        list: A list of created reader projects described as a dictionary with tags
            "container", "id", and "new" as described in define_container above.
    """

    # I want a list of group permissions with rw and ro only:\
    # TODO: Group permissions may be changing
    group_readers = [
        perm.id for perm in group.permissions if perm.access in ["rw", "ro"]
    ]

    # Retrieve a list of all projects in this group
    group_projects = fw_client.projects.find(f'group="{group.id}"')

    # Keep track of the created containers, in case of "rollback"
    created_data = []

    # Keep track of the reader projects we need to create and the max_cases for each
    readers_to_instantiate = []

    # Update or create reader-projects from a provided csv file
    # readers_csv is a path to a csv file with columns:
    # "email", "first_name", "last_name", and "max_cases"
    if readers_csv and os.path.exists(readers_csv):

        # Load dataframe from file
        readers_df = pd.read_csv(readers_csv)

        # Validate that dataframe has required columns before proceeding
        req_columns = ["email", "first_name", "last_name", "max_cases"]
        if all([(c in readers_df.columns) for c in req_columns]):
            # update max_cases for existing projects in the reader group according to
            # csv data
            update_reader_projects_metadata(fw_client, group_projects, readers_df)

            # identify new readers, instantiate, give group permissions
            readers_to_instantiate = instantiate_new_readers(
                fw_client, group, group_readers, readers_df
            )

        else:
            log.warning(
                'The csv-file "%s" did not have the required columns("%s"). '
                "Proceeding without reader CSV.",
                readers_csv,
                '", "'.join(req_columns),
            )

    # The following assumes that the resultant project will have only one non-admin user
    # 'read-write' if they are currently editing
    # 'read-only' if they are complete with their tasks

    proj_roles = [
        role.id
        for role in fw_client.get_all_roles()
        if role.label in ["read-write", "read-only"]
    ]

    reader_projects = [
        [
            perm.id
            for perm in proj.permissions
            if set(perm.role_ids).intersection(proj_roles)
        ][0]
        for proj in group_projects
    ]

    # Extend "readers_to_instantiate" with list of readers with 'rw' group permissions,
    # but are not in the group's projects

    # group users needing projects instantiated
    group_readers_new_projects = list(
        set(group_readers).difference(set(reader_projects))
    )

    # If readers_to_instantiate is empty (no new readers from csv), create reader
    # projects for all
    # readers in group without one
    if not readers_to_instantiate:
        readers_to_instantiate = [
            (reader, max_cases) for reader in group_readers_new_projects
        ]
    else:
        # else, prevent duplicate reader project creation
        pending_new_reader_projects = [
            reader for (reader, max_cases) in readers_to_instantiate
        ]
        readers_to_instantiate.extend(
            [
                (reader, max_cases)
                for reader in group_readers_new_projects
                if reader not in pending_new_reader_projects
            ]
        )

    ohif_config_path = None
    if readers_to_instantiate:
        ohif_config_path = "/tmp/ohif_config.json"
        if master_project.get_file("ohif_config.json"):
            master_project.download_file("ohif_config.json", ohif_config_path)
        else:
            ohif_config_path = None

    for reader, _max_cases in readers_to_instantiate:
        reader_number = len(group.projects()) + 1
        project_label = "Reader " + str(reader_number)
        project_info = {
            "project_features": {"assignments": [], "max_cases": _max_cases}
        }

        new_project, created_container = create_project(
            fw_client, project_label, group, reader, project_info
        )
        if ohif_config_path and os.path.exists(ohif_config_path):
            new_project.upload_file(ohif_config_path)

        created_data.append(created_container)

    return created_data


def initialize_dataframes(fw_client, reader_group):
    """
    Initializes pandas DataFrames used to select sessions and reader projects

    Args:
        fw_client (flywheel.Client): Flywheel Client object instantiated on instance
        reader_group (flywheel.Group): The reader group

    Returns:
        tuple: a pair of pandas DataFrames representing the source sessions and
            destination projects
    """

    # This dataframe is to keep track of the sessions each reader project has and the
    # total number of those sessions. Initialized below.
    dest_projects_df = pd.DataFrame(
        columns=[
            "id",
            "label",
            "reader_id",
            "assignments",
            "max_cases",
            "num_assignments",
        ],
        dtype="object",
    )

    # Initialize destination projects dataframe
    for reader_proj in fw_client.projects.find(f'group="{reader_group.id}"'):
        reader_proj = reader_proj.reload()
        project_features = reader_proj.info["project_features"]
        # Valid roles for readers are "read-write" and "read-only"
        proj_roles = [
            role.id
            for role in fw_client.get_all_roles()
            if role.label in ["read-write", "read-only"]
        ]
        reader_id = [
            perm.id
            for perm in reader_proj.permissions
            if set(perm.role_ids).intersection(proj_roles)
        ][0]
        # Fill the dataframe with project data.
        dest_projects_df.loc[dest_projects_df.shape[0] + 1] = [
            reader_proj.id,
            reader_proj.label,
            reader_id,
            project_features["assignments"],
            project_features["max_cases"],
            len(reader_proj.sessions()),
        ]

    # This dataframe keeps track of each reader project and session each session was
    # exported to.
    source_sessions_df = pd.DataFrame(
        columns=["id", "label", "assignments", "assigned_count"]
    )

    return source_sessions_df, dest_projects_df


def select_readers_without_replacement(session_features, dest_projects_df):
    """
    Select reader projects to export assigned sessions to based on
        "selection without replacement"

    Args:
        session_features (dict): Current session's features used to assign and export
            to multiple reader projects
        dest_projects_df (pandas.DataFrame): Dataframe recording projects and their
            assigned sessions

    Returns:
        list: A list of ids from reader projects to populate with a given session
    """

    # Select case_coverage readers for each case
    # If avail_case_coverage == 0 we don't need to look.  It is all full up.
    avail_case_coverage = session_features["case_coverage"] - len(
        session_features["assignments"]
    )

    # add it to case_coverage distinct readers (projects)
    readers_proj_assigned = [
        assignments["project_id"] for assignments in session_features["assignments"]
    ]

    df_temp = dest_projects_df[
        (dest_projects_df.num_assignments == np.min(dest_projects_df.num_assignments))
        & (dest_projects_df.num_assignments < dest_projects_df.max_cases)
        & ~dest_projects_df.id.isin(readers_proj_assigned)
    ]

    min_avail_coverage = min(avail_case_coverage, df_temp.shape[0])
    # new readers to assign a session
    assign_reader_projs = list(
        np.random.choice(df_temp.id, min_avail_coverage, replace=False)
    )

    if df_temp.shape[0] < avail_case_coverage:
        # save the length of the previous df_temp
        df_temp_len = df_temp.shape[0]
        # Select all but the above readers_proj_assigned
        df_temp = dest_projects_df[
            ~dest_projects_df.id.isin(readers_proj_assigned + assign_reader_projs)
            & (dest_projects_df.num_assignments < dest_projects_df.max_cases)
        ]
        assign_reader_projs.extend(
            list(
                np.random.choice(
                    df_temp.id,
                    # need to choose the minimum of these
                    min(avail_case_coverage - df_temp_len, df_temp.shape[0]),
                    replace=False,
                )
            )
        )

    return assign_reader_projs


def assign_single_case(fw_client, src_session, reader_group_id, reader_id, reason):

    src_project = fw_client.get(src_session.parents["project"]).reload()

    # Grab project-level features, if it does not exist, set defaults
    project_features = src_project.get("project_features")

    # Keep track of all the exported and created data
    # On Failure, remove contents of created_data from instance.
    exported_data = []
    created_data = []

    # Find or create reader group
    reader_group, _created_data = find_or_create_group(
        fw_client, reader_group_id, "Readers"
    )
    created_data.extend(_created_data)

    # Initialize dataframes used to select sessions and readers without replacement
    source_sessions_df, dest_projects_df = initialize_dataframes(
        fw_client, reader_group
    )

    # find record with reader_id
    indx = dest_projects_df[dest_projects_df.reader_id == reader_id].index
    project_id = dest_projects_df.id[indx]
    reader_proj = fw_client.get(project_id).reload()

    session_features = set_session_features(src_session, 3)

    if reason in ["Breaking a Tie", "Individual Assignment"]:
        # Check reader availability
        if dest_projects_df.num_assignments[indx] == dest_projects_df.max_cases[indx]:
            log.error(
                "Cannot assign more than %s cases to %s. "
                "Consider increasing max_cases for this reader "
                "or choosing another reader.",
                dest_projects_df.max_cases[indx],
                reader_id,
            )
            raise Exception("Max assignments reached.")

        # check for the existence of the selected session in the reader project
        if src_session.label in [sess.label for sess in reader_proj.sessions()]:
            log.error(
                "Selected session (%s) has already been assigned to reader (%s).",
                src_session.label,
                reader_id,
            )
            raise Exception("Existing Session in Destination Project.")

        # Increment case_coverage, if necessary
        if session_features["assigned_count"] == session_features["case_coverage"]:
            session_features["case_coverage"] += 1

        try:
            # export the session to the reader project
            dest_session, _exported_data, _created_data = export_session(
                fw_client, src_session, fw_client.get(project_id)
            )

            exported_data.extend(_exported_data)
            created_data.extend(_created_data)

        except Exception as e:
            log.warning("Error while exporting a session, %s.", src_session.label)
            log.exception(e)
            log.warning("Examine the data and try again.")

        if not dest_projects_df.loc[indx, "assignments"]:
            dest_projects_df.loc[indx, "assignments"] = [
                {"source_session": src_session.id, "dest_session": dest_session.id}
            ]
        else:
            dest_projects_df.loc[indx, "assignments"].append(
                {"source_session": src_session.id, "dest_session": dest_session.id}
            )

        dest_projects_df.loc[indx, "num_assignments"] += 1
        session_features["assigned_count"] += 1
        session_features["assignments"].append(
            {
                "project_id": project_id,
                "reader_id": reader_id,
                "session_id": dest_session.id,
                "status": "Assigned",
            }
        )

    else:
        """
        Here we may want to give the reader a bit more of a description of how this is
        going to go.
        """
        # check for the existence of the selected session in the reader project
        if src_session.label not in [sess.label for sess in reader_proj.sessions()]:
            log.error(
                "Selected session (%s) must be assigned to reader (%s) to update.",
                src_session.label,
                reader_id,
            )
            raise Exception("Missing Session in Destination Project.")

        # Find reader_assignment
        assignment = [
            assignment
            for assignment in session_features["assignments"]
            if assignment["reader_id"] == reader_id
        ][0]
        # Set status back to "Assigned" and removed any assessment data, if it exists
        assignment["status"] = "Assigned"
        if assignment.get("read"):
            assignment.pop("read")
        if assignment.get("measurements"):
            assignment.pop("measurements")

        dest_session = fw_client.get(assignment["session_id"]).reload()
        # Update ohifViewer object...
        # TODO: Check with Jody to ensure that this can:
        #       1. be marked as "unread"
        #       2. give some indication to the reader that they need to
        #          correct their assessment
        dest_ohifViewer = dest_session.info["ohifViewer"]
        _reader_id = reader_id.replace(".", "_")
        for k, _ in dest_ohifViewer["read"][_reader_id]["notes"].copy().items():
            dest_ohifViewer["read"][_reader_id]["notes"].pop(k)
        dest_ohifViewer["read"][_reader_id]["notes"]["additionalNotes"] = reason

        dest_session.update_info({"ohifViewer": dest_ohifViewer})

    # This is where we give the "source" the information about where it went
    # later, we will want to use this to query "completed sessions" and get
    # ready for the next phase.
    session_info = {"session_features": session_features}

    # Restore the session_features to the source session
    src_session.update_info(session_info)
    # Iterate through sessions to record system state
    for tmp_session in src_project.sessions():
        tmp_session = tmp_session.reload()
        session_features = tmp_session.info.get("session_features")
        # always record the state in the dataframe.
        session_features["id"] = tmp_session.id
        session_features["label"] = tmp_session.label
        source_sessions_df = source_sessions_df.append(
            session_features, ignore_index=True
        )

        project_session_attributes = set_project_session_attributes(session_features)

        # Check to see if the case is already present in the project_features
        case = [
            case
            for case in project_features["case_states"]
            if case["id"] == session_features["id"]
        ]
        if case:
            index = project_features["case_states"].index(case[0])
            project_features["case_states"].pop(index)

        # append new or updated case data to project_features
        project_features["case_states"].append(project_session_attributes)

    # TODO: There was a proposal to include "reader_states" in here as well.  That
    #       would put this update_info down below the next loop.
    #       And it would put some extra elements within the loop as well.
    src_project.update_info({"project_features": project_features})

    # update reader project from updates to the dataframe
    project_info = {
        "project_features": {
            "assignments": dest_projects_df.loc[indx, "assignments"],
            "max_cases": dest_projects_df.loc[indx, "max_cases"],
        }
    }
    reader_proj.update_info(project_info)

    """
     Depending on "reason", we are going to want to assign or reassign a single case. and depending on
     those actions, there will be some constraints to adhere to 

        
     2. Likewise, if this is a "reassignment", then the case MUST exist in the reader project.  Enforced with a graceful exit.
        Also, with this "reassignment", we want to notify the user, somehow, that this case has been reassigned for some reason.  the custom-info.ohifViewer.read object does not do this well.

    """

    return source_sessions_df, dest_projects_df, exported_data_df


def distribute_cases_to_readers(fw_client, src_project, reader_group_id, case_coverage):
    """
    Distribute cases (sessions) from a source project to multiple reader projects.

    Each case (session) is exported to case_coverage selected readers until the
    max_cases of each reader is achieved or all sessions have been distributed.
    Readers are selected from a pool of available readers (readers that have less than
    reader.max_cases assigned) without replacement. Readers with the least number of
    sessions assigned are assigned new sessions first.

    This function can be run multiple times with new sessions in the source project and
    new readers created with the `assign-readers` gear.

    Args:
        fw_client (flywheel.Client): An instantiated Flywheel Client to host instance
        src_project_label (str): The label of the source project for all sessions
        reader_group_id (str): The Flywheel container id for the group in question
        case_coverage (int): The default number of readers assigned to each session

    Returns:
        tuple: Pandas DataFrames recording source and destination for
            each session exported.
    """

    # Grab project-level features, if it does not exist, set defaults
    project_features = (
        src_project.info["project_features"]
        if src_project.get("project_features")
        else {"case_coverage": case_coverage, "case_states": []}
    )

    src_sessions = src_project.sessions()

    # Keep track of all the exported and created data
    # On Failure, remove contents of created_data from instance.
    exported_data = []
    created_data = []

    # Find or create reader group
    reader_group, _created_data = find_or_create_group(
        fw_client, reader_group_id, "Readers"
    )
    created_data.extend(_created_data)

    # Initialize dataframes used to select sessions and readers without replacement
    source_sessions_df, dest_projects_df = initialize_dataframes(
        fw_client, reader_group
    )

    # If the dataframe for destination projects is empty, raise an error.
    if dest_projects_df.shape[0] == 0:
        raise NoReaderProjectsError(
            "Readers have not been added to this project. "
            "Please run `assign-readers` with valid configuration first."
        )

    # for each session in the sessions found
    for src_session in src_sessions:
        # Reload to capture all metadata
        src_session = src_session.reload()
        session_features = set_session_features(src_session, case_coverage)

        # select available readers to receive the session
        assign_reader_projs = select_readers_without_replacement(
            session_features, dest_projects_df
        )

        # This is where we record which readers receive the session
        # and export that session to each of those readers.
        # Iterate through the assign_reader_projs, export the session to each of them,
        # record results
        for project_id in assign_reader_projs:
            # grab the reader_id from the selected project
            project = fw_client.get(project_id)

            proj_roles = [
                role.id
                for role in fw_client.get_all_roles()
                if role.label in ["read-write", "read-only"]
            ]

            reader_id = [
                perm.id
                for perm in project.permissions
                if set(perm.role_ids).intersection(proj_roles)
            ][0]
            try:
                # export the session to the reader project
                dest_session, _exported_data, _created_data = export_session(
                    fw_client, src_session, fw_client.get(project_id)
                )

                exported_data.extend(_exported_data)
                created_data.extend(_created_data)

            except Exception as e:
                log.warning("Error while exporting a session, %s.", src_session.label)
                log.exception(e)
                log.warning("Examine the data and try again.")

            # grab the index from the dataframe and record source and dest
            # session ids
            indx = dest_projects_df[dest_projects_df.id == project_id].index[0]
            if not dest_projects_df.loc[indx, "assignments"]:
                dest_projects_df.loc[indx, "assignments"] = [
                    {"source_session": src_session.id, "dest_session": dest_session.id}
                ]
            else:
                dest_projects_df.loc[indx, "assignments"].append(
                    {"source_session": src_session.id, "dest_session": dest_session.id}
                )

            dest_projects_df.loc[indx, "num_assignments"] += 1
            session_features["assigned_count"] += 1
            session_features["assignments"].append(
                {
                    "project_id": project_id,
                    "reader_id": reader_id,
                    "session_id": dest_session.id,
                    "status": "Assigned",
                }
            )

        # This is where we give the "source" the information about where it went
        # later, we will want to use this to query "completed sessions" and get
        # ready for the next phase.
        session_info = {"session_features": session_features}

        # Restore the session_features to the source session
        src_session.update_info(session_info)

        # always record the state in the dataframe.
        session_features["id"] = src_session.id
        session_features["label"] = src_session.label
        source_sessions_df = source_sessions_df.append(
            session_features, ignore_index=True
        )

        project_session_attributes = set_project_session_attributes(session_features)

        # Check to see if the case is already present in the project_features
        case = [
            case
            for case in project_features["case_states"]
            if case["id"] == session_features["id"]
        ]
        if case:
            index = project_features["case_states"].index(case[0])
            project_features["case_states"].pop(index)

        # append new or updated case data to project_features
        project_features["case_states"].append(project_session_attributes)

    # TODO: There was a proposal to include "reader_states" in here as well.  That
    #       would put this update_info down below the next loop.
    #       And it would put some extra elements within the loop as well.
    src_project.update_info({"project_features": project_features})

    # Iterate through all of the readers and update their metadata:
    for indx in dest_projects_df.index:
        project_id = dest_projects_df.loc[indx, "id"]
        reader_proj = fw_client.get(project_id)
        project_info = {
            "project_features": {
                "assignments": dest_projects_df.loc[indx, "assignments"],
                "max_cases": dest_projects_df.loc[indx, "max_cases"],
            }
        }
        reader_proj.update_info(project_info)

    # Create a DataFrame from exported_data and then export
    exported_data_df = pd.DataFrame(data=exported_data)

    return source_sessions_df, dest_projects_df, exported_data_df
